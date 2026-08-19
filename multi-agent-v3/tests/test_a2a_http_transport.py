from __future__ import annotations

import json
from typing import Any, cast

import pytest
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import (
    Message,
    Part,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
)
from a2a.types.a2a_pb2 import (
    Task as ProtoTask,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct, Value
from misaka_a2a_capability import A2AAgentCard, A2ASkill, TaskStatus
from misaka_a2a_http import (
    A2AHttpConfig,
    SDKRequestHandler,
    agent_card_to_proto,
    create_a2a_http_app,
    task_request_from_proto,
)
from misaka_a2a_runtime import A2AServer, A2AServerStatus, DelegationTaskHandler
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_delegation_runtime import DelegationRuntime
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_invocation_contracts import CapabilityFeature
from misaka_invocation_runtime import InvocationRuntime
from starlette.testclient import TestClient


def _card() -> A2AAgentCard:
    features = frozenset(
        {
            CapabilityFeature.STRUCTURED_OUTPUT,
            CapabilityFeature.STREAMING,
            CapabilityFeature.CANCELLATION,
        }
    )
    return A2AAgentCard(
        agent_id="fake-a2a-node",
        name="Fake A2A Node",
        description="Standalone official-SDK transport test node",
        version="0.1.0",
        skills=(
            A2ASkill(
                skill_id="agent.invoke",
                name="Invoke agent",
                description="Execute one local agent task",
                capability_id=AGENT_CAPABILITY_ID,
                operation=AGENT_OPERATION_INVOKE,
                features=features,
                required_task_fields=frozenset({"provider_id", "model", "effort"}),
            ),
        ),
        features=features,
    )


def _message_payload(task_id: str) -> dict[str, object]:
    return {
        "messageId": f"message-{task_id}",
        "contextId": "context-http",
        "taskId": task_id,
        "role": "ROLE_USER",
        "parts": [{"data": {"prompt": "Return a deterministic answer"}}],
        "metadata": {
            "capabilityId": AGENT_CAPABILITY_ID,
            "operation": AGENT_OPERATION_INVOKE,
            "providerId": "fake-agent",
            "model": "fake/model",
            "effort": "high",
            "requiredFeatures": ["streaming"],
            "outputSchema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
    }


def _proto_request(task_id: str) -> SendMessageRequest:
    metadata_payload = _message_payload(task_id)["metadata"]
    if not isinstance(metadata_payload, dict):
        raise AssertionError("test message metadata must be an object")
    metadata = ParseDict(
        cast(dict[str, Any], metadata_payload),
        Struct(),
    )
    data = ParseDict({"prompt": "hello"}, Value())
    return SendMessageRequest(
        message=Message(
            message_id=f"message-{task_id}",
            context_id="context-http",
            task_id=task_id,
            role=Role.ROLE_USER,
            parts=[Part(data=data)],
            metadata=metadata,
        )
    )


def _sse_payloads(lines: list[str]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("data: "):
            payloads.append(cast(dict[str, Any], json.loads(line.removeprefix("data: "))))
    return payloads


def test_sdk_mapper_requires_explicit_execution_selection() -> None:
    request = task_request_from_proto(_proto_request("task-map"))

    assert request.capability_id == AGENT_CAPABILITY_ID
    assert request.provider_id == "fake-agent"
    assert request.model == "fake/model"
    assert request.effort == "high"
    assert request.input == {"prompt": "hello"}

    missing = _proto_request("task-invalid")
    missing.message.metadata.fields.pop("model")
    assert task_request_from_proto(missing).model is None


@pytest.mark.asyncio
async def test_sdk_subscription_reconnects_from_header_sequence() -> None:
    provider = FakeAgentProvider(FakeAgentScenario(events=({"type": "progress", "step": 1},)))
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)
    delegation_runtime = DelegationRuntime(runtime, MemoryInteractionChannelStore())
    card = _card()
    server = A2AServer(DelegationTaskHandler(delegation_runtime, card, provider_id="fake-agent"))
    await server.start()
    try:
        handler = SDKRequestHandler(server)
        task = await handler.on_message_send(_proto_request("task-reconnect"), ServerCallContext())
        assert isinstance(task, ProtoTask)
        assert task.id == "task-reconnect"

        context = ServerCallContext(state={"headers": {"last-event-id": "2"}})
        events = [
            event
            async for event in handler.on_subscribe_to_task(
                SubscribeToTaskRequest(id="task-reconnect"),
                context,
            )
        ]
        sequences = [MessageToDict(event.metadata)["sequence"] for event in events]

        assert sequences
        assert sequences[0] == 3
    finally:
        await server.stop()
        await delegation_runtime.stop()
        await runtime.stop()


def test_official_http_jsonrpc_and_sse_routes_share_internal_task_facts() -> None:
    provider = FakeAgentProvider(
        FakeAgentScenario(
            output={"answer": "transport-ok"},
            events=({"type": "progress", "step": 1},),
        )
    )
    runtime = InvocationRuntime()
    delegation_runtime = DelegationRuntime(runtime, MemoryInteractionChannelStore())
    card = _card()
    server = A2AServer(DelegationTaskHandler(delegation_runtime, card, provider_id="fake-agent"))

    async def start() -> None:
        await runtime.register_provider("fake-agent", provider)
        await server.start()

    async def stop() -> None:
        await server.stop()
        await delegation_runtime.stop()
        await runtime.stop()

    app = create_a2a_http_app(
        server,
        card,
        config=A2AHttpConfig(public_url="http://127.0.0.1:8015"),
        start=start,
        stop=stop,
    )
    headers = {"A2A-Version": "1.0"}

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.json()["a2aServer"] == "active"

        card_response = client.get("/.well-known/agent-card.json")
        assert card_response.status_code == 200
        assert card_response.json()["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"

        response = client.post(
            "/a2a/message:send",
            headers=headers,
            json={"message": _message_payload("task-http")},
        )
        assert response.status_code == 200, response.text
        task = response.json()["task"]
        assert task["id"] == "task-http"
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert task["metadata"]["invocationId"] != task["id"]
        assert task["metadata"]["delegationId"] != task["id"]
        assert task["metadata"]["delegationId"] != task["metadata"]["invocationId"]
        assert task["metadata"]["activationId"] != task["metadata"]["invocationId"]
        assert task["metadata"]["activationId"] != task["metadata"]["delegationId"]

        rpc = client.post(
            "/a2a",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": "get-1",
                "method": "GetTask",
                "params": {"id": "task-http"},
            },
        )
        assert rpc.status_code == 200, rpc.text
        assert rpc.json()["result"]["id"] == "task-http"

        with client.stream(
            "POST",
            "/a2a/message:stream",
            headers=headers,
            json={"message": _message_payload("task-stream")},
        ) as stream:
            payloads = _sse_payloads(list(stream.iter_lines()))
        assert payloads
        assert payloads[-1]["statusUpdate"]["status"]["state"] == ("TASK_STATE_COMPLETED")

        with client.stream(
            "GET",
            "/a2a/tasks/task-stream:subscribe",
            headers={**headers, "X-A2A-Start-Sequence": "3"},
        ) as resumed:
            resumed_payloads = _sse_payloads(list(resumed.iter_lines()))
        assert resumed_payloads
        assert resumed_payloads[0]["statusUpdate"]["metadata"]["sequence"] == 3

    assert server.status is A2AServerStatus.STOPPED
    assert server.active_task_count == 0
    assert provider.starts == 2


def test_task_state_mapper_preserves_reconciliation_attention() -> None:
    card = _card()
    proto_card = agent_card_to_proto(card, interface_url="http://127.0.0.1:8015/a2a")

    assert proto_card.capabilities.streaming is True
    extension = MessageToDict(proto_card.capabilities.extensions[0].params)
    assert extension["maxInputBytes"] == card.max_input_bytes
    assert TaskStatus.RECONCILIATION_REQUIRED.value == "reconciliation_required"
