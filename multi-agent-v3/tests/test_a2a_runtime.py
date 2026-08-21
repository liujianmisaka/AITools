from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from misaka_a2a_capability import (
    A2AAgentCard,
    A2AServerStateError,
    A2ASkill,
    TaskCapabilityRejected,
    TaskExecutionHandle,
    TaskIdempotencyConflict,
    TaskRequest,
    TaskStatus,
)
from misaka_a2a_runtime import A2AServer, DelegationTaskHandler
from misaka_agent_capability import AGENT_CAPABILITY_ID
from misaka_delegation_runtime import DelegationRuntime
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario, FakeFailure
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_invocation_contracts import (
    CapabilityFeature,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ReconcileResult,
    ReconcileStatus,
    SessionRef,
)
from misaka_invocation_runtime import InvocationRuntime
from misaka_kernel_contracts import JsonObject
from misaka_persistence_jsonl import JsonlEventLog, JsonlSessionLog


def _card(*, max_input_bytes: int = 1024) -> A2AAgentCard:
    features = frozenset(
        {
            CapabilityFeature.STRUCTURED_OUTPUT,
            CapabilityFeature.STREAMING,
            CapabilityFeature.CANCELLATION,
            CapabilityFeature.RESUME,
        }
    )
    return A2AAgentCard(
        agent_id="fake-a2a-agent",
        name="Fake A2A Agent",
        description="Deterministic standalone test agent",
        version="0.1.0",
        skills=(
            A2ASkill(
                skill_id="agent.invoke",
                name="Invoke agent",
                description="Run a local agent invocation",
                capability_id=AGENT_CAPABILITY_ID,
                operation="invoke",
                features=features,
                required_task_fields=frozenset({"provider_id", "model", "effort"}),
            ),
        ),
        features=features,
        max_input_bytes=max_input_bytes,
    )


def _request(
    task_id: str = "task-1",
    *,
    idempotency_key: str = "idem-1",
    prompt: str = "hello",
    required_features: frozenset[CapabilityFeature] = frozenset(),
) -> TaskRequest:
    return TaskRequest(
        task_id=task_id,
        context_id="context-1",
        message_id="message-1",
        idempotency_key=idempotency_key,
        capability_id=AGENT_CAPABILITY_ID,
        operation="invoke",
        input={"prompt": prompt},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        required_features=required_features,
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )


async def _server(
    scenario: FakeAgentScenario | None = None,
    *,
    max_input_bytes: int = 1024,
) -> tuple[A2AServer, DelegationRuntime, InvocationRuntime, FakeAgentProvider]:
    provider = FakeAgentProvider(scenario)
    runtime = InvocationRuntime(
        cancellation_timeout_seconds=0.5,
        shutdown_timeout_seconds=0.5,
    )
    await runtime.register_provider("fake-agent", provider)
    delegation_runtime = DelegationRuntime(
        runtime,
        MemoryInteractionChannelStore(),
    )
    server = A2AServer(
        DelegationTaskHandler(
            delegation_runtime,
            _card(max_input_bytes=max_input_bytes),
            provider_id="fake-agent",
        ),
        shutdown_timeout_seconds=0.5,
    )
    await server.start()
    return server, delegation_runtime, runtime, provider


async def _stop(
    server: A2AServer,
    delegation_runtime: DelegationRuntime,
    runtime: InvocationRuntime,
) -> None:
    await server.stop()
    await delegation_runtime.stop()
    await runtime.stop()


@pytest.mark.asyncio
async def test_a2a_server_executes_task_and_keeps_task_invocation_ids_distinct() -> None:
    server, delegation_runtime, runtime, provider = await _server(
        FakeAgentScenario(
            output={"answer": "done"},
            events=({"type": "agent.progress", "percent": 50},),
        )
    )
    try:
        handle = await server.submit(
            _request(required_features=frozenset({CapabilityFeature.STREAMING}))
        )
        result = await handle.wait()
        snapshot = await server.snapshot("task-1")

        assert result.status is TaskStatus.COMPLETED
        assert result.output == {"answer": "done"}
        assert result.invocation_id == handle.invocation_id
        assert result.delegation_id == handle.delegation_id
        assert result.activation_id == handle.activation_id
        assert snapshot.delegation_id == handle.delegation_id
        assert snapshot.activation_id == handle.activation_id
        assert result.invocation_id != "task-1"
        assert result.delegation_id != result.invocation_id
        assert result.activation_id not in {result.delegation_id, result.invocation_id}
        assert snapshot.request.task_id == "task-1"
        assert provider.starts == 1
        assert any(
            event.payload.get("delegation_id") == result.delegation_id for event in snapshot.events
        )
        message_event = next(event for event in snapshot.events if "message_type" in event.payload)
        assert message_event.payload["sender_id"] == (f"delegation:{result.delegation_id}")
        assert message_event.payload["delivery_status"] == "accepted"
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_server_reuses_task_for_duplicate_idempotency_key() -> None:
    server, delegation_runtime, runtime, provider = await _server()
    try:
        first = await server.submit(_request())
        duplicate = await server.submit(_request("task-retry"))
        first_result, duplicate_result = await asyncio.gather(first.wait(), duplicate.wait())

        assert first_result == duplicate_result
        assert duplicate_result.task_id == "task-1"
        assert first.delegation_id == duplicate.delegation_id
        assert provider.starts == 1
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_server_rejects_idempotency_conflict() -> None:
    server, delegation_runtime, runtime, _ = await _server()
    try:
        await server.submit(_request())
        with pytest.raises(TaskIdempotencyConflict):
            await server.submit(_request("task-2", prompt="different"))
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_server_rejects_unsupported_feature_before_provider_start() -> None:
    server, delegation_runtime, runtime, provider = await _server()
    try:
        with pytest.raises(TaskCapabilityRejected, match="artifacts"):
            await server.submit(
                _request(required_features=frozenset({CapabilityFeature.ARTIFACTS}))
            )
        assert provider.starts == 0
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_server_enforces_input_size_before_provider_start() -> None:
    server, delegation_runtime, runtime, provider = await _server(max_input_bytes=16)
    try:
        with pytest.raises(TaskCapabilityRejected, match="exceeds"):
            await server.submit(_request(prompt="x" * 100))
        assert provider.starts == 0
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_server_requires_skill_execution_fields_before_provider_start() -> None:
    server, delegation_runtime, runtime, provider = await _server()
    try:
        request = _request()
        missing_model = TaskRequest(
            task_id=request.task_id,
            context_id=request.context_id,
            message_id=request.message_id,
            idempotency_key=request.idempotency_key,
            capability_id=request.capability_id,
            operation=request.operation,
            input=request.input,
            provider_id=request.provider_id,
            model=None,
            effort=request.effort,
            required_features=request.required_features,
            output_schema=request.output_schema,
        )
        with pytest.raises(TaskCapabilityRejected, match="model"):
            await server.submit(missing_model)
        assert provider.starts == 0
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_context_session_reference_is_accepted_but_not_forwarded() -> None:
    server, delegation_runtime, runtime, provider = await _server()
    try:
        handle = await server.submit(
            replace(
                _request(),
                session_ref=SessionRef(provider="a2a", native_id="context-1"),
            )
        )
        result = await handle.wait()

        assert result.status is TaskStatus.COMPLETED
        invocation = await runtime.store.snapshot(result.invocation_id or "")
        assert invocation.request.session_ref is None
        assert provider.starts == 1
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_context_session_reference_must_match_context() -> None:
    server, delegation_runtime, runtime, provider = await _server()
    try:
        handle = await server.submit(
            replace(
                _request(),
                session_ref=SessionRef(provider="a2a", native_id="other-context"),
            )
        )
        result = await handle.wait()

        assert result.status is TaskStatus.REJECTED
        assert result.error_code == "a2a.context_session_mismatch"
        assert provider.starts == 0
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_tasks_reuse_context_delegation_without_replaying_events() -> None:
    server, delegation_runtime, runtime, provider = await _server(
        FakeAgentScenario(events=({"type": "progress", "step": 1},))
    )
    try:
        first = await server.submit(_request("task-first", idempotency_key="idem-first"))
        first_result = await first.wait()
        first_events = [event async for event in first.events()]
        second_request = replace(
            _request(
                "task-second",
                idempotency_key="idem-second",
                prompt="continue",
            ),
            message_id="message-second",
        )
        second = await server.submit(second_request)
        second_result = await second.wait()
        second_events = [event async for event in second.events()]

        assert first_result.status is TaskStatus.COMPLETED
        assert second_result.status is TaskStatus.COMPLETED
        assert first_result.delegation_id == second_result.delegation_id
        assert first_result.invocation_id != second_result.invocation_id
        assert first_result.activation_id != second_result.activation_id
        assert [event.sequence for event in first_events] == list(range(1, len(first_events) + 1))
        assert [event.sequence for event in second_events] == list(range(1, len(second_events) + 1))
        second_message_events = [
            event for event in second_events if "channel_sequence" in event.payload
        ]
        second_messages = [
            cast(JsonObject, event.payload["message"]) for event in second_message_events
        ]
        assert second_message_events
        assert all(
            message.get("activation_id") == second_result.activation_id
            for message in second_messages
        )
        assert all(
            message.get("activation_id") != first_result.activation_id
            for message in second_messages
        )
        delegation = await delegation_runtime.snapshot(first.delegation_id or "")
        assert delegation.activation_count == 2
        assert delegation.ref.session_id == "a2a-session:fake-a2a-agent:context-1"
        assert provider.starts == 2
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_context_rejects_fixed_configuration_drift() -> None:
    server, delegation_runtime, runtime, provider = await _server()
    try:
        first = await server.submit(_request("task-first", idempotency_key="idem-first"))
        await first.wait()
        second = await server.submit(
            replace(
                _request("task-second", idempotency_key="idem-second"),
                message_id="message-second",
                model="fake/other-model",
            )
        )
        result = await second.wait()

        assert result.status is TaskStatus.REJECTED
        assert result.error_code == "a2a.context_configuration_mismatch"
        assert provider.starts == 1
    finally:
        await _stop(server, delegation_runtime, runtime)


class _RecordingExecution:
    def __init__(
        self,
        request: InvocationRequest,
        provider_id: str,
        native_session_id: str,
    ) -> None:
        self.invocation_id = request.invocation_id
        self.activation_id = f"{request.invocation_id}:provider-activation"
        self._provider_id = provider_id
        self._native_session_id = native_session_id

    async def events(self) -> AsyncIterator[InvocationEvent]:
        for sequence, status in enumerate(
            (InvocationStatus.RUNNING, InvocationStatus.SUCCEEDED),
            start=1,
        ):
            yield InvocationEvent(
                invocation_id=self.invocation_id,
                sequence=sequence,
                status=status,
                payload={
                    "provider_id": self._provider_id,
                    "provider_session_id": self._native_session_id,
                    "provider_operation_id": f"operation-{self.invocation_id}",
                },
            )

    async def wait(self) -> InvocationResult:
        return InvocationResult(
            invocation_id=self.invocation_id,
            status=InvocationStatus.SUCCEEDED,
            output={"answer": "recorded"},
        )

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("reason must not be empty")

    async def reconcile(self) -> ReconcileResult:
        return ReconcileResult(ReconcileStatus.SUCCEEDED)


class _RecordingExecutionPort:
    def __init__(self, native_session_id: str = "provider-session-1") -> None:
        self.native_session_id = native_session_id
        self.requests: list[InvocationRequest] = []

    async def submit(
        self,
        request: InvocationRequest,
        *,
        provider_id: str | None = None,
    ) -> _RecordingExecution:
        self.requests.append(request)
        return _RecordingExecution(
            request,
            provider_id or "fake-agent",
            self.native_session_id,
        )


@pytest.mark.asyncio
async def test_a2a_follow_up_receives_persisted_provider_session(tmp_path: Path) -> None:
    port = _RecordingExecutionPort()
    delegation_runtime = DelegationRuntime(
        port,
        MemoryInteractionChannelStore(),
        session_log=JsonlSessionLog(JsonlEventLog(tmp_path / "sessions.jsonl")),
        composition_id="a2a-test",
    )
    server = A2AServer(
        DelegationTaskHandler(
            delegation_runtime,
            _card(),
            provider_id="fake-agent",
        )
    )
    await server.start()
    try:
        first = await server.submit(
            _request(
                "task-first",
                idempotency_key="idem-first",
                required_features=frozenset({CapabilityFeature.RESUME}),
            )
        )
        await first.wait()
        second = await server.submit(
            replace(
                _request(
                    "task-second",
                    idempotency_key="idem-second",
                    required_features=frozenset({CapabilityFeature.RESUME}),
                ),
                message_id="message-second",
            )
        )
        await second.wait()

        assert len(port.requests) == 2
        assert port.requests[0].session_ref is None
        assert port.requests[1].session_ref == SessionRef(
            provider="fake-agent",
            native_id="provider-session-1",
        )
        assert CapabilityFeature.RESUME not in port.requests[1].required_features
    finally:
        await server.stop()
        await delegation_runtime.stop()


@pytest.mark.asyncio
async def test_concurrent_first_context_submissions_create_one_delegation() -> None:
    provider = FakeAgentProvider(FakeAgentScenario(delay_seconds=0.05))
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)
    delegation_runtime = DelegationRuntime(runtime, MemoryInteractionChannelStore())
    handler = DelegationTaskHandler(
        delegation_runtime,
        _card(),
        provider_id="fake-agent",
    )
    first_request = _request("task-first", idempotency_key="idem-first")
    second_request = replace(
        _request("task-second", idempotency_key="idem-second"),
        message_id="message-second",
    )
    try:
        results = await asyncio.gather(
            handler.submit(first_request),
            handler.submit(second_request),
            return_exceptions=True,
        )
        handles = [result for result in results if not isinstance(result, BaseException)]
        errors = [result for result in results if isinstance(result, BaseException)]

        assert len(handles) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], TaskCapabilityRejected)
        assert getattr(errors[0], "code", None) == "a2a.context_busy"
        assert len(await delegation_runtime.store.list()) == 1
        await handles[0].wait()
    finally:
        await delegation_runtime.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_a2a_delegation_rejects_provider_override_on_fixed_node() -> None:
    server, delegation_runtime, runtime, provider = await _server()
    try:
        handle = await server.submit(replace(_request(), provider_id="other-agent"))
        result = await handle.wait()

        assert result.status is TaskStatus.REJECTED
        assert result.error_code == "a2a.provider_mismatch"
        assert provider.starts == 0
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_delegation_preserves_pre_activation_provider_failure() -> None:
    server, delegation_runtime, runtime, provider = await _server(
        FakeAgentScenario(failure=FakeFailure("fake.start_failed", "provider refused to start"))
    )
    try:
        handle = await server.submit(_request())
        result = await handle.wait()

        assert result.status is TaskStatus.FAILED
        assert result.delegation_id == handle.delegation_id
        assert result.error_code == "fake.start_failed"
        assert provider.starts == 1
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_task_events_support_reconnect() -> None:
    server, delegation_runtime, runtime, _ = await _server(
        FakeAgentScenario(
            events=(
                {"type": "progress", "step": 1},
                {"type": "progress", "step": 2},
            )
        )
    )
    try:
        handle = await server.submit(_request())
        await handle.wait()
        all_events = [event async for event in handle.events()]
        resumed = [event async for event in handle.events(start_sequence=3)]

        assert [event.sequence for event in resumed] == list(range(3, len(all_events) + 1))
        assert resumed[-1].status is TaskStatus.COMPLETED
    finally:
        await _stop(server, delegation_runtime, runtime)


@pytest.mark.asyncio
async def test_a2a_task_can_be_cancelled_and_server_rejects_after_stop() -> None:
    server, delegation_runtime, runtime, provider = await _server(
        FakeAgentScenario(delay_seconds=0.2)
    )
    handle = await server.submit(_request())
    await provider.started.wait()
    await handle.cancel("user cancelled")
    result = await handle.wait()

    assert result.status is TaskStatus.CANCELLED
    await server.stop()
    assert server.active_task_count == 0
    with pytest.raises(A2AServerStateError):
        await server.submit(_request("task-after-stop", idempotency_key="after-stop"))
    await delegation_runtime.stop()
    await runtime.stop()


class _BlockingHandler:
    async def describe(self) -> A2AAgentCard:
        return _card()

    async def submit(self, request: TaskRequest) -> TaskExecutionHandle:
        del request
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_a2a_stop_waits_for_bounded_inflight_submission() -> None:
    server = A2AServer(
        _BlockingHandler(),
        submission_timeout_seconds=0.02,
        shutdown_timeout_seconds=0.2,
    )
    await server.start()
    submit = asyncio.create_task(server.submit(_request()))
    await asyncio.sleep(0)
    first_stop = asyncio.create_task(server.stop())
    second_stop = asyncio.create_task(server.stop())

    handle, _, _ = await asyncio.gather(submit, first_stop, second_stop)
    result = await handle.wait()

    assert result.status is TaskStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "a2a.handler_submit_timeout"
    assert server.active_task_count == 0
