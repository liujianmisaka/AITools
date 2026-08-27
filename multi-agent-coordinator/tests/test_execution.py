import asyncio
import json
from collections.abc import Mapping

import httpx
import pytest

from misaka_coordinator_service.execution import (
    DelegationCancelRequest,
    DelegationMessageRequest,
    DelegationMode,
    DelegationReconciliationRequest,
    DelegationRequest,
    DelegationStatus,
    ExecutionProviderCatalog,
    ExecutionSelection,
    MessageDelivery,
    MessageDispatchSnapshot,
    ReconciliationStatus,
    SessionStreamEvent,
    SessionStreamEventKind,
    V3ActorKind,
    V3ExecutionContractError,
    V3ExecutionGateway,
    V3ExecutionGatewayError,
    V3ProtocolError,
    V3SessionGateway,
    V3SessionGatewayConfig,
    V3SessionGatewayError,
    V3SessionHTTPError,
    V3SessionProtocolError,
    V3ToolInvocationError,
    V3ToolUnavailableError,
)
from misaka_coordinator_service.tools import (
    ToolCallResult,
    ToolNotAvailableError,
    ToolRegistryError,
)


def snapshot_payload(
    *,
    delegation_id: str = "delegation-1",
    status: str = "active",
    revision: int = 1,
    session_id: str | None = "session-1",
    invocation_id: str | None = "invocation-1",
    activation_id: str | None = "activation-1",
    terminal: bool | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "delegation_id": delegation_id,
        "status": status,
        "revision": revision,
        "session_id": session_id,
        "channel_id": f"channel:{delegation_id}",
        "parent_delegation_id": None,
        "depth": 0,
        "current_invocation_id": invocation_id,
        "current_activation_id": activation_id,
        "activation_count": 1,
        "child_delegation_ids": [],
        "report": None,
    }
    if terminal is not None:
        payload["terminal"] = terminal
    return payload


def session_payload(*, delegation_id: str = "delegation-1") -> dict[str, object]:
    return {
        "delegation": snapshot_payload(delegation_id=delegation_id),
        "provider_id": "codex",
        "model": "pixel/gpt-5.6-luna",
        "effort": "medium",
        "provider_session_id": "provider-session-1",
        "provider_operation_id": "provider-operation-1",
        "activation_number": 1,
        "last_sequence": 2,
        "stage": "running",
        "closed": False,
        "updated_at": "2026-08-27T08:00:00Z",
    }


def event_payload(*, sequence: int = 1, delegation_id: str = "delegation-1") -> dict[str, object]:
    return {
        "delegation_id": delegation_id,
        "sequence": sequence,
        "kind": "output_delta",
        "invocation_id": "invocation-1",
        "activation_id": "activation-1",
        "activation_number": 1,
        "status": "active",
        "provider_session_id": "provider-session-1",
        "provider_operation_id": "provider-operation-1",
        "payload": {"text": "partial"},
        "occurred_at": "2026-08-27T08:00:00Z",
    }


class FakeToolCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.responses: dict[str, object] = {}
        self.fail_with: Exception | None = None

    async def invoke(self, tool_name: str, arguments: Mapping[str, object]) -> ToolCallResult:
        self.calls.append((tool_name, dict(arguments)))
        if self.fail_with is not None:
            raise self.fail_with
        value = self.responses[tool_name]
        return ToolCallResult(
            invocation_id=f"call-{len(self.calls)}",
            tool_name=tool_name,
            source_id="v3",
            value=value,
        )


def selection() -> ExecutionSelection:
    return ExecutionSelection(
        provider_id="codex",
        model="pixel/gpt-5.6-luna",
        effort="medium",
    )


def delegation_request() -> DelegationRequest:
    return DelegationRequest(
        prompt="审查实现",
        cwd="D:/dev/project",
        selection=selection(),
        mode=DelegationMode.CONTINUABLE,
        delegation_id="delegation-1",
        idempotency_key="idempotency-1",
        session_id="session-1",
        channel_id="channel:delegation-1",
        input={"topic": "sdk"},
        required_features=("structured_output",),
        policy={"network_policy": "deny"},
    )


def test_execution_request_rejects_gateway_owned_input_and_bad_reconciliation() -> None:
    with pytest.raises(V3ExecutionContractError, match="reserved fields"):
        DelegationRequest(
            prompt="run",
            cwd="D:/dev/project",
            selection=selection(),
            input={"cwd": "D:/other"},
        )
    with pytest.raises(V3ExecutionContractError, match="64 lowercase"):
        DelegationRequest(
            prompt="run",
            cwd="D:/dev/project",
            selection=selection(),
            plan_hash="bad",
        )
    with pytest.raises(V3ExecutionContractError, match="provided together"):
        DelegationMessageRequest(
            delegation_id="d",
            session_id="s",
            message="continue",
            model="pixel/model",
        )
    with pytest.raises(V3ExecutionContractError, match="only valid"):
        DelegationReconciliationRequest(
            delegation_id="d",
            expected_revision=1,
            status=ReconciliationStatus.FAILED,
            reason="failed",
            output={"unexpected": True},
        )


def test_gateway_maps_all_command_operations_to_stable_models() -> None:
    caller = FakeToolCaller()
    caller.responses = {
        "delegate_task": snapshot_payload(status="admitted"),
        "get_task_status": snapshot_payload(revision=2),
        "wait_task": snapshot_payload(status="completed", terminal=True, revision=3),
        "list_tasks": {"tasks": [snapshot_payload(), snapshot_payload(delegation_id="d2")]},
        "send_task_message": {
            "dispatch_id": "dispatch-1",
            "delegation_id": "delegation-1",
            "session_id": "session-1",
            "status": "applied",
            "revision": 1,
            "applied_strategy": "append",
            "previous_activation_id": "activation-1",
            "current_activation_id": "activation-2",
            "error_code": None,
            "error_message": None,
        },
        "cancel_task": snapshot_payload(status="cancelled", terminal=True, revision=4),
        "resolve_task_reconciliation": snapshot_payload(
            status="completed", terminal=True, revision=5
        ),
        "list_execution_options": {
            "providers": [
                {
                    "provider_id": "codex",
                    "models": [
                        {
                            "model_id": "pixel/gpt-5.6-luna",
                            "display_name": "Luna",
                            "description": "test",
                            "supported_efforts": ["low", "medium"],
                        }
                    ],
                }
            ]
        },
    }
    gateway = V3ExecutionGateway(tools=caller)

    delegated = asyncio.run(gateway.delegate(delegation_request()))
    current = asyncio.run(gateway.get("delegation-1"))
    waited = asyncio.run(gateway.wait("delegation-1", timeout_ms=10))
    listed = asyncio.run(gateway.list(status=DelegationStatus.ACTIVE, limit=2))
    dispatched = asyncio.run(
        gateway.send_message(
            DelegationMessageRequest(
                delegation_id="delegation-1",
                session_id="session-1",
                message="补充来源",
                delivery=MessageDelivery.INTERRUPT_CONTINUE,
                expected_activation_id="activation-1",
            )
        )
    )
    cancelled = asyncio.run(
        gateway.cancel(
            DelegationCancelRequest(
                delegation_id="delegation-1",
                reason="用户取消",
                session_id="session-1",
            )
        )
    )
    reconciled = asyncio.run(
        gateway.resolve_reconciliation(
            DelegationReconciliationRequest(
                delegation_id="delegation-1",
                expected_revision=4,
                status=ReconciliationStatus.COMPLETED,
                reason="已核验",
                output={"answer": "ok"},
            )
        )
    )
    options = asyncio.run(gateway.execution_options())

    assert delegated.status is DelegationStatus.ADMITTED
    assert current.revision == 2
    assert waited.status is DelegationStatus.COMPLETED
    assert tuple(item.delegation_id for item in listed) == ("delegation-1", "d2")
    assert isinstance(dispatched, MessageDispatchSnapshot)
    assert cancelled.status is DelegationStatus.CANCELLED
    assert reconciled.status is DelegationStatus.COMPLETED
    assert isinstance(options[0], ExecutionProviderCatalog)
    assert options[0].models[0].supported_efforts == ("low", "medium")

    names = [name for name, _arguments in caller.calls]
    assert names == [
        "delegate_task",
        "get_task_status",
        "wait_task",
        "list_tasks",
        "send_task_message",
        "cancel_task",
        "resolve_task_reconciliation",
        "list_execution_options",
    ]
    delegate_arguments = caller.calls[0][1]
    assert delegate_arguments["provider_id"] == "codex"
    assert delegate_arguments["mode"] == "continuable"
    assert delegate_arguments["wait_timeout_ms"] == 0


def test_gateway_normalizes_tool_failures_protocol_errors_and_bounds() -> None:
    caller = FakeToolCaller()
    caller.responses["get_task_status"] = {"status": "active"}
    gateway = V3ExecutionGateway(tools=caller)
    with pytest.raises(V3ProtocolError, match="delegation_id"):
        asyncio.run(gateway.get("d"))
    with pytest.raises(V3ExecutionGatewayError, match="timeout_ms"):
        asyncio.run(gateway.wait("d", timeout_ms=-1))
    with pytest.raises(V3ExecutionGatewayError, match="limit"):
        asyncio.run(gateway.list(limit=101))

    caller.fail_with = ToolNotAvailableError("not exposed")
    with pytest.raises(V3ToolUnavailableError, match="unavailable"):
        asyncio.run(gateway.get("d"))
    caller.fail_with = ToolRegistryError("internal detail")
    with pytest.raises(V3ToolInvocationError, match="failed") as captured:
        asyncio.run(gateway.get("d"))
    assert "internal detail" not in str(captured.value)


def test_gateway_accepts_json_text_and_maf_content_results() -> None:
    caller = FakeToolCaller()
    caller.responses["get_task_status"] = json.dumps(snapshot_payload())
    gateway = V3ExecutionGateway(tools=caller)
    assert asyncio.run(gateway.get("d")).delegation_id == "delegation-1"

    from agent_framework import Content

    caller.responses["get_task_status"] = [Content.from_text("not-json")]
    with pytest.raises(V3ProtocolError, match="invalid JSON"):
        asyncio.run(gateway.get("d"))


def test_terminal_delegation_keeps_worker_session_reference_for_follow_up() -> None:
    terminal = snapshot_payload(
        status="completed",
        revision=2,
        invocation_id=None,
        activation_id=None,
        terminal=True,
    )
    caller = FakeToolCaller()
    caller.responses["get_task_status"] = terminal
    snapshot = asyncio.run(V3ExecutionGateway(tools=caller).get("delegation-1"))

    assert snapshot.execution_reference.invocation_id is None
    assert snapshot.execution_reference.activation_id is None
    assert snapshot.execution_reference.worker_session_id == "session-1"


def test_session_gateway_parses_history_and_sse_stream() -> None:
    stream_body = (
        "retry: 3000\n\n"
        f"event: delegation.session.snapshot\nid: 0\ndata: {json.dumps(session_payload())}\n\n"
        f"event: delegation.session.event\nid: 1\ndata: {json.dumps(event_payload())}\n\n"
        f"event: delegation.session.end\ndata: "
        f"{json.dumps({'delegation_id': 'delegation-1', 'next_sequence': 2})}\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["actor_id"] == "coordinator"
        assert request.url.params["actor_kind"] == "agent"
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json=session_payload())
        if request.url.path.endswith("/session/events"):
            return httpx.Response(200, json=[event_payload(sequence=1), event_payload(sequence=2)])
        return httpx.Response(200, text=stream_body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = V3SessionGateway(
        config=V3SessionGatewayConfig(
            control_plane_url="http://127.0.0.1:8016/",
            actor_id="coordinator",
            actor_kind=V3ActorKind.AGENT,
        ),
        client=client,
    )
    snapshot = asyncio.run(gateway.get_session("delegation-1"))
    events = asyncio.run(gateway.list_events("delegation-1", next_sequence=1))

    async def collect() -> list[SessionStreamEvent]:
        return [event async for event in gateway.stream_events("delegation-1", next_sequence=1)]

    streamed = asyncio.run(collect())
    asyncio.run(gateway.aclose())

    assert snapshot.provider_id == "codex"
    assert tuple(event.sequence for event in events) == (1, 2)
    assert streamed[0].kind is SessionStreamEventKind.SNAPSHOT
    assert streamed[1].kind is SessionStreamEventKind.EVENT
    assert streamed[1].session_event is not None
    assert streamed[1].session_event.payload["text"] == "partial"
    assert streamed[2].kind is SessionStreamEventKind.END
    assert streamed[2].next_sequence == 2


def test_session_gateway_rejects_bad_http_and_sse_payloads() -> None:
    def bad_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session/events"):
            return httpx.Response(200, json=[event_payload(sequence=2), event_payload(sequence=1)])
        if request.url.path.endswith("/session/stream"):
            return httpx.Response(200, text="event: unknown\ndata: {}\n\n")
        return httpx.Response(503, text="service unavailable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(bad_handler))
    gateway = V3SessionGateway(
        config=V3SessionGatewayConfig(
            control_plane_url="http://127.0.0.1:8016", actor_id="coordinator"
        ),
        client=client,
    )
    with pytest.raises(V3SessionHTTPError, match="HTTP 503"):
        asyncio.run(gateway.get_session("delegation-1"))
    with pytest.raises(V3SessionProtocolError, match="strictly increasing"):
        asyncio.run(gateway.list_events("delegation-1"))

    async def collect() -> list[SessionStreamEvent]:
        return [event async for event in gateway.stream_events("delegation-1")]

    with pytest.raises(V3SessionProtocolError, match="unsupported"):
        asyncio.run(collect())
    with pytest.raises(V3SessionGatewayError, match="next_sequence"):
        asyncio.run(gateway.list_events("delegation-1", next_sequence=True))
    asyncio.run(gateway.aclose())


def test_session_gateway_config_and_contracts_are_strict() -> None:
    with pytest.raises(V3SessionGatewayError, match="absolute HTTP"):
        V3SessionGatewayConfig(control_plane_url="bad", actor_id="coordinator")
    with pytest.raises(V3SessionGatewayError, match="greater than zero"):
        V3SessionGatewayConfig(
            control_plane_url="http://127.0.0.1:8016",
            actor_id="coordinator",
            request_timeout_seconds=0,
        )


def test_session_gateway_validates_ids_and_encodes_path_segments() -> None:
    observed_paths: list[str] = []

    def encoded_handler(request: httpx.Request) -> httpx.Response:
        observed_paths.append(request.url.raw_path.decode("ascii"))
        return httpx.Response(
            200,
            json=session_payload(delegation_id="delegation/with slash"),
        )

    encoded_client = httpx.AsyncClient(transport=httpx.MockTransport(encoded_handler))
    encoded_gateway = V3SessionGateway(
        config=V3SessionGatewayConfig(
            control_plane_url="http://127.0.0.1:8016",
            actor_id="coordinator",
        ),
        client=encoded_client,
    )
    asyncio.run(encoded_gateway.get_session("delegation/with slash"))
    assert observed_paths == [
        "/delegations/delegation%2Fwith%20slash/session?actor_id=coordinator&actor_kind=agent",
    ]
    asyncio.run(encoded_gateway.aclose())

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=session_payload(delegation_id="other"))
        )
    )
    gateway = V3SessionGateway(
        config=V3SessionGatewayConfig(
            control_plane_url="http://127.0.0.1:8016",
            actor_id="coordinator",
        ),
        client=client,
    )
    with pytest.raises(V3SessionProtocolError, match="snapshot delegation_id"):
        asyncio.run(gateway.get_session("delegation-1"))
    asyncio.run(gateway.aclose())


def test_session_gateway_rejects_mismatched_history_and_snapshot_events() -> None:
    def history_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[event_payload(delegation_id="other")])

    history_client = httpx.AsyncClient(transport=httpx.MockTransport(history_handler))
    history_gateway = V3SessionGateway(
        config=V3SessionGatewayConfig(
            control_plane_url="http://127.0.0.1:8016",
            actor_id="coordinator",
        ),
        client=history_client,
    )
    with pytest.raises(V3SessionProtocolError, match="event delegation_id"):
        asyncio.run(history_gateway.list_events("delegation-1"))
    asyncio.run(history_gateway.aclose())

    snapshot_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                text=(
                    "event: delegation.session.snapshot\n"
                    f"data: {json.dumps(session_payload(delegation_id='other'))}\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )
        )
    )
    snapshot_gateway = V3SessionGateway(
        config=V3SessionGatewayConfig(
            control_plane_url="http://127.0.0.1:8016",
            actor_id="coordinator",
        ),
        client=snapshot_client,
    )

    async def collect() -> list[SessionStreamEvent]:
        return [event async for event in snapshot_gateway.stream_events("delegation-1")]

    with pytest.raises(V3SessionProtocolError, match="snapshot delegation_id"):
        asyncio.run(collect())
    asyncio.run(snapshot_gateway.aclose())
