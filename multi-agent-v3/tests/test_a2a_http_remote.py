from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from a2a.types.a2a_pb2 import (
    Message,
    Part,
    Role,
    StreamResponse,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import Parse, ParseDict
from google.protobuf.struct_pb2 import Struct, Value
from google.protobuf.timestamp_pb2 import Timestamp
from misaka_a2a_capability import A2AAgentCard, A2ASkill, TaskIdempotencyConflict, TaskRequest
from misaka_a2a_capability import TaskStatus as DomainTaskStatus
from misaka_a2a_http import (
    A2AHttpClient,
    A2AHttpConfig,
    A2AHttpTaskClient,
    agent_card_from_proto,
    agent_card_to_proto,
    create_a2a_http_app,
    task_event_from_proto,
    task_result_from_proto,
)
from misaka_a2a_provider import A2AInvocationProvider, A2AProviderConfig
from misaka_a2a_runtime import A2AServer, DelegationTaskHandler
from misaka_delegation_runtime import DelegationRuntime
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_invocation_contracts import CapabilityFeature, CompletionBoundary, InvocationRequest
from misaka_invocation_runtime import InvocationRuntime


def _card() -> A2AAgentCard:
    features = frozenset(
        {
            CapabilityFeature.STRUCTURED_OUTPUT,
            CapabilityFeature.STREAMING,
            CapabilityFeature.CANCELLATION,
        }
    )
    return A2AAgentCard(
        agent_id="remote-card",
        name="Remote Card",
        description="Mapper fixture",
        version="1.0.0",
        skills=(
            A2ASkill(
                skill_id="agent.invoke",
                name="Invoke",
                description="Invoke",
                capability_id="agent.invocation",
                operation="invoke",
                output_schema={"type": "object"},
                features=features,
                required_task_fields=frozenset({"model", "effort"}),
            ),
        ),
        features=features,
    )


def _request(task_id: str = "task-remote") -> TaskRequest:
    return TaskRequest(
        task_id=task_id,
        context_id="context-remote",
        message_id=f"message-{task_id}",
        idempotency_key=f"idem-{task_id}",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        model="remote/model",
        effort="high",
        output_schema={"type": "object"},
        provider_id="fake-agent",
    )


def _timestamp(value: datetime) -> Timestamp:
    timestamp = Timestamp()
    timestamp.FromDatetime(value)
    return timestamp


def _metadata(value: dict[str, object]) -> Struct:
    return ParseDict(value, Struct())


def _completed_task(request: TaskRequest) -> Task:
    return Task(
        id=request.task_id,
        context_id=request.context_id,
        status=TaskStatus(
            state=TaskState.TASK_STATE_COMPLETED,
            message=Message(
                message_id=f"result-{request.task_id}",
                context_id=request.context_id,
                task_id=request.task_id,
                role=Role.ROLE_AGENT,
                parts=[Part(data=Parse(json.dumps({"answer": "ok"}), Value()))],
            ),
            timestamp=_timestamp(datetime.now(UTC)),
        ),
        metadata=_metadata(
            {
                "taskStatus": "completed",
                "invocationId": "invocation-remote",
                "delegationId": "delegation-remote",
                "activationId": "activation-remote",
            }
        ),
        history=[],
    )


def test_agent_card_proto_round_trip_preserves_capability_metadata() -> None:
    proto = agent_card_to_proto(_card(), interface_url="http://127.0.0.1:8015/a2a")

    restored = agent_card_from_proto(proto)

    assert restored.agent_id == "remote-card"
    assert restored.version == "1.0.0"
    assert restored.skills[0].capability_id == "agent.invocation"
    assert restored.skills[0].operation == "invoke"
    assert restored.skills[0].required_task_fields == frozenset({"model", "effort"})
    assert CapabilityFeature.STREAMING in restored.features


def test_task_proto_mappers_preserve_sequence_and_structured_result() -> None:
    occurred_at = datetime.now(UTC)
    update = TaskStatusUpdateEvent(
        task_id="task-remote",
        context_id="context-remote",
        status=TaskStatus(
            state=TaskState.TASK_STATE_WORKING,
            timestamp=_timestamp(occurred_at),
        ),
        metadata=_metadata(
            {
                "sequence": 2,
                "taskStatus": "working",
                "payload": {"step": 2},
            }
        ),
    )
    event = task_event_from_proto(
        StreamResponse(status_update=update),
        task_id="task-remote",
        fallback_sequence=1,
    )
    assert event is not None
    assert event.sequence == 2
    assert event.status is DomainTaskStatus.WORKING
    assert event.payload["step"] == 2.0

    result = task_result_from_proto(_completed_task(_request()))
    assert result is not None
    assert result.status is DomainTaskStatus.COMPLETED
    assert result.output == {"answer": "ok"}


class _FakeRawA2AClient:
    def __init__(self) -> None:
        self.card = agent_card_to_proto(_card(), interface_url="http://remote/a2a")
        self.requests: list[TaskRequest] = []
        self.closed = False

    async def connect(self) -> None:
        return

    async def stream(self, request: TaskRequest) -> AsyncIterator[StreamResponse]:
        self.requests.append(request)
        update = TaskStatusUpdateEvent(
            task_id=request.task_id,
            context_id=request.context_id,
            status=TaskStatus(
                state=TaskState.TASK_STATE_WORKING,
                timestamp=_timestamp(datetime.now(UTC)),
            ),
            metadata=_metadata(
                {
                    "sequence": 1,
                    "taskStatus": "working",
                    "payload": {"step": 1},
                }
            ),
        )
        yield StreamResponse(status_update=update)

    async def get(self, task_id: str) -> Task:
        request = next(item for item in self.requests if item.task_id == task_id)
        return _completed_task(request)

    async def cancel(self, task_id: str) -> Task:
        request = next(item for item in self.requests if item.task_id == task_id)
        task = _completed_task(request)
        task.status.state = TaskState.TASK_STATE_CANCELED
        task.metadata = _metadata({"taskStatus": "cancelled"})
        return task

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_http_task_client_adapts_stream_and_terminal_task() -> None:
    raw = _FakeRawA2AClient()
    client = A2AHttpTaskClient(cast(A2AHttpClient, raw))

    assert (await client.describe()).agent_id == "remote-card"
    handle = await client.submit(_request())
    events = [event async for event in handle.events()]
    result = await handle.wait()

    assert [event.sequence for event in events] == [1]
    assert result.status is DomainTaskStatus.COMPLETED
    assert result.output == {"answer": "ok"}
    snapshot = await client.get("task-remote")
    assert snapshot.result == result

    await client.close()
    assert raw.closed is True


@pytest.mark.asyncio
async def test_http_task_client_rejects_conflicting_duplicate_task() -> None:
    raw = _FakeRawA2AClient()
    client = A2AHttpTaskClient(cast(A2AHttpClient, raw))
    request = _request()
    await client.submit(request)

    with pytest.raises(TaskIdempotencyConflict):
        await client.submit(replace(request, input={"prompt": "different"}))

    await client.close()


@pytest.mark.asyncio
async def test_http_task_client_runs_against_a_local_a2a_server() -> None:
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "server-ok"}))
    invocation_runtime = InvocationRuntime()
    await invocation_runtime.register_provider("fake-agent", provider)
    delegation_runtime = DelegationRuntime(
        invocation_runtime,
        MemoryInteractionChannelStore(),
    )
    server = A2AServer(
        DelegationTaskHandler(
            delegation_runtime,
            _card(),
            provider_id="fake-agent",
        )
    )
    await server.start()
    app = create_a2a_http_app(
        server,
        _card(),
        config=A2AHttpConfig(public_url="http://testserver"),
    )
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    remote = A2AHttpTaskClient(A2AHttpClient("http://testserver", http_client=http_client))
    try:
        described = await remote.describe()
        assert described.agent_id == "remote-card"
        handle = await remote.submit(_request("task-local-server"))
        events = [event async for event in handle.events()]
        result = await handle.wait()
        assert events
        assert result.status is DomainTaskStatus.COMPLETED
        assert result.output == {"answer": "server-ok"}
    finally:
        await remote.close()
        await server.stop()
        await delegation_runtime.stop()
        await invocation_runtime.stop()


@pytest.mark.asyncio
async def test_remote_a2a_provider_runs_through_invocation_runtime_and_http() -> None:
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "provider-ok"}))
    server_runtime = InvocationRuntime()
    await server_runtime.register_provider("fake-agent", provider)
    server_delegation = DelegationRuntime(
        server_runtime,
        MemoryInteractionChannelStore(),
    )
    server = A2AServer(
        DelegationTaskHandler(
            server_delegation,
            _card(),
            provider_id="fake-agent",
        )
    )
    await server.start()
    app = create_a2a_http_app(
        server,
        _card(),
        config=A2AHttpConfig(public_url="http://provider-test"),
    )
    raw_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://provider-test",
    )
    remote = A2AHttpTaskClient(A2AHttpClient("http://provider-test", http_client=raw_http))
    caller_runtime = InvocationRuntime()
    caller_provider = A2AInvocationProvider(
        remote,
        config=A2AProviderConfig(
            provider_id="remote-a2a",
            remote_provider_id="fake-agent",
        ),
    )
    await caller_runtime.register_provider("remote-a2a", caller_provider)
    try:
        request = InvocationRequest(
            invocation_id="caller-invocation-1",
            capability_id="agent.invocation",
            operation="invoke",
            input={"prompt": "hello remote"},
            idempotency_key="caller-idempotency-1",
            completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
            output_schema={"type": "object"},
            model="remote/model",
            effort="high",
        )
        handle = await caller_runtime.submit(request, provider_id="remote-a2a")
        result = await handle.wait()
        assert result.output == {"answer": "provider-ok"}
    finally:
        await caller_runtime.stop()
        await remote.close()
        await server.stop()
        await server_delegation.stop()
        await server_runtime.stop()
