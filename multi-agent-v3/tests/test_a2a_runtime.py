from __future__ import annotations

import asyncio

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
from misaka_a2a_runtime import A2AServer, InvocationTaskHandler
from misaka_agent_capability import AGENT_CAPABILITY_ID
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_contracts import CapabilityFeature
from misaka_invocation_runtime import InvocationRuntime


def _card(*, max_input_bytes: int = 1024) -> A2AAgentCard:
    features = frozenset(
        {
            CapabilityFeature.STRUCTURED_OUTPUT,
            CapabilityFeature.STREAMING,
            CapabilityFeature.CANCELLATION,
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
) -> tuple[A2AServer, InvocationRuntime, FakeAgentProvider]:
    provider = FakeAgentProvider(scenario)
    runtime = InvocationRuntime(
        cancellation_timeout_seconds=0.5,
        shutdown_timeout_seconds=0.5,
    )
    await runtime.register_provider("fake-agent", provider)
    server = A2AServer(
        InvocationTaskHandler(
            runtime, _card(max_input_bytes=max_input_bytes), provider_id="fake-agent"
        ),
        shutdown_timeout_seconds=0.5,
    )
    await server.start()
    return server, runtime, provider


@pytest.mark.asyncio
async def test_a2a_server_executes_task_and_keeps_task_invocation_ids_distinct() -> None:
    server, runtime, provider = await _server(
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
        assert result.invocation_id != "task-1"
        assert snapshot.request.task_id == "task-1"
        assert provider.starts == 1
        assert any(event.payload.get("type") == "agent.progress" for event in snapshot.events)
    finally:
        await server.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_a2a_server_reuses_task_for_duplicate_idempotency_key() -> None:
    server, runtime, provider = await _server()
    try:
        first = await server.submit(_request())
        duplicate = await server.submit(_request("task-retry"))
        first_result, duplicate_result = await asyncio.gather(first.wait(), duplicate.wait())

        assert first_result == duplicate_result
        assert duplicate_result.task_id == "task-1"
        assert provider.starts == 1
    finally:
        await server.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_a2a_server_rejects_idempotency_conflict() -> None:
    server, runtime, _ = await _server()
    try:
        await server.submit(_request())
        with pytest.raises(TaskIdempotencyConflict):
            await server.submit(_request("task-2", prompt="different"))
    finally:
        await server.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_a2a_server_rejects_unsupported_feature_before_provider_start() -> None:
    server, runtime, provider = await _server()
    try:
        with pytest.raises(TaskCapabilityRejected, match="resume"):
            await server.submit(_request(required_features=frozenset({CapabilityFeature.RESUME})))
        assert provider.starts == 0
    finally:
        await server.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_a2a_server_enforces_input_size_before_provider_start() -> None:
    server, runtime, provider = await _server(max_input_bytes=16)
    try:
        with pytest.raises(TaskCapabilityRejected, match="exceeds"):
            await server.submit(_request(prompt="x" * 100))
        assert provider.starts == 0
    finally:
        await server.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_a2a_task_events_support_reconnect() -> None:
    server, runtime, _ = await _server(
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
        await server.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_a2a_task_can_be_cancelled_and_server_rejects_after_stop() -> None:
    server, runtime, provider = await _server(FakeAgentScenario(delay_seconds=0.2))
    handle = await server.submit(_request())
    await provider.started.wait()
    await handle.cancel("user cancelled")
    result = await handle.wait()

    assert result.status is TaskStatus.CANCELLED
    await server.stop()
    assert server.active_task_count == 0
    with pytest.raises(A2AServerStateError):
        await server.submit(_request("task-after-stop", idempotency_key="after-stop"))
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
