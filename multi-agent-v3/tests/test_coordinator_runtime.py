from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from misaka_coordinator_adapters import InvocationExecutionPlan, JsonlEventDeliveryStore
from misaka_coordinator_runtime import (
    CoordinatorConflict,
    CoordinatorStatus,
    DirectCoordinator,
    EventDeliveryStatus,
    EventEnvelope,
    ExecutionEvent,
    ExecutionResult,
    ExecutionStatus,
    MemoryEventDeliveryStore,
    MemoryEventSource,
    QueueCapacityExceeded,
    QueueCoordinator,
    QueueJobStatus,
    ReactiveCoordinator,
    ReconciliationResult,
    ReconciliationState,
    start_execution,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario, FakeFailure
from misaka_invocation_contracts import CompletionBoundary, InvocationRequest
from misaka_invocation_runtime import InvocationRuntime
from misaka_persistence_jsonl import JsonlEventLog


def _request(invocation_id: str, *, key: str | None = None) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": invocation_id},
        idempotency_key=key or invocation_id,
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        model="fake/model",
        effort="high",
    )


async def _runtime(
    scenario: FakeAgentScenario | None = None,
) -> tuple[InvocationRuntime, FakeAgentProvider]:
    provider = FakeAgentProvider(scenario)
    runtime = InvocationRuntime(cancellation_timeout_seconds=0.5, shutdown_timeout_seconds=0.5)
    await runtime.register_provider("fake", provider)
    return runtime, provider


def _plan(
    runtime: InvocationRuntime,
    request: InvocationRequest,
    *,
    provider_id: str = "fake",
) -> InvocationExecutionPlan:
    return InvocationExecutionPlan(runtime, request, provider_id=provider_id)


class _ControlledHandle:
    def __init__(
        self,
        execution_id: str,
        result: ExecutionResult | None = None,
    ) -> None:
        self.execution_id = execution_id
        self.activation_id = f"{execution_id}:activation:1"
        self._result = result
        self._done = asyncio.Event()
        self.cancel_reason: str | None = None
        if result is not None:
            self._done.set()

    def events(self, *, start_sequence: int = 1) -> AsyncIterator[ExecutionEvent]:
        async def _events() -> AsyncIterator[ExecutionEvent]:
            if start_sequence > 1:
                return
            result = await self.wait()
            yield ExecutionEvent(
                execution_id=result.execution_id,
                sequence=1,
                status=result.status,
            )

        return _events()

    async def wait(self) -> ExecutionResult:
        await self._done.wait()
        assert self._result is not None
        return self._result

    async def cancel(self, reason: str) -> None:
        self.cancel_reason = reason
        if self._result is None:
            self._result = ExecutionResult(
                execution_id=self.execution_id,
                activation_id=self.activation_id,
                status=ExecutionStatus.CANCELLED,
                error_code="fake.cancelled",
                error_message=reason,
            )
            self._done.set()

    async def reconcile(self) -> ReconciliationResult:
        result = await self.wait()
        state = {
            ExecutionStatus.SUCCEEDED: ReconciliationState.SUCCEEDED,
            ExecutionStatus.CANCELLED: ReconciliationState.CANCELLED,
            ExecutionStatus.FAILED: ReconciliationState.FAILED,
            ExecutionStatus.RECONCILIATION_REQUIRED: ReconciliationState.UNREACHABLE,
        }[result.status]
        return ReconciliationResult(state=state)


class _ControlledPlan:
    def __init__(self, handle: _ControlledHandle, *, gate: asyncio.Event | None = None) -> None:
        self.execution_id = handle.execution_id
        self.fingerprint = handle.execution_id
        self.handle = handle
        self.gate = gate
        self.start_count = 0

    async def start(self, *, attempt: int = 1) -> _ControlledHandle:
        self.start_count += 1
        if attempt < 1:
            raise ValueError("attempt must be at least one")
        if self.gate is not None:
            await self.gate.wait()
        return self.handle


@pytest.mark.asyncio
async def test_memory_event_source_deduplicates_filters_and_replays() -> None:
    source = MemoryEventSource()
    first = await source.publish("git.commit", {"sha": "a"}, event_id="event-a")
    duplicate = await source.publish("git.commit", {"sha": "other"}, event_id="event-a")
    await source.publish("cron", {"name": "nightly"}, event_id="event-b")
    await source.close()

    assert duplicate == first
    all_events = [event async for event in source.events()]
    commits = [event async for event in source.events(topic="git.commit")]
    resumed = [event async for event in source.events(start_sequence=2)]
    assert [event.event_id for event in all_events] == ["event-a", "event-b"]
    assert [event.event_id for event in commits] == ["event-a"]
    assert [event.sequence for event in resumed] == [2]


@pytest.mark.asyncio
async def test_start_execution_cleans_up_when_caller_is_cancelled_during_start() -> None:
    release = asyncio.Event()
    handle = _ControlledHandle("start-race")
    plan = _ControlledPlan(handle, gate=release)
    task = asyncio.create_task(
        start_execution(
            plan,
            attempt=1,
            cancellation_reason="caller cancelled",
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert handle.cancel_reason == "caller cancelled"


@pytest.mark.asyncio
async def test_queue_rejects_zero_max_attempts_instead_of_using_default() -> None:
    runtime, _ = await _runtime()
    coordinator = QueueCoordinator(default_max_attempts=2)
    await coordinator.start()
    with pytest.raises(ValueError, match="max_attempts"):
        await coordinator.submit(
            "job-invalid-attempts",
            _plan(runtime, _request("invalid")),
            max_attempts=0,
        )
    await coordinator.stop()
    await runtime.stop()


@pytest.mark.asyncio
async def test_direct_coordinator_submits_and_stops_without_stopping_shared_runtime() -> None:
    runtime, _ = await _runtime()
    coordinator = DirectCoordinator(shutdown_timeout_seconds=0.5)
    await coordinator.start()
    handle = await coordinator.submit(_plan(runtime, _request("direct-1")))
    result = await handle.wait()
    assert result.status is ExecutionStatus.SUCCEEDED
    assert coordinator.status is CoordinatorStatus.ACTIVE
    await coordinator.stop()
    assert coordinator.status is CoordinatorStatus.STOPPED
    await runtime.stop()


@pytest.mark.asyncio
async def test_reactive_routes_duplicates_and_factory_errors() -> None:
    runtime, provider = await _runtime()
    event_source = MemoryEventSource()

    async def route(event: EventEnvelope) -> InvocationExecutionPlan | None:
        if event.event_id == "bad":
            raise ValueError("bad route")
        return _plan(runtime, _request(f"reactive-{event.event_id}"))

    coordinator = ReactiveCoordinator(
        event_source,
        route,
        max_concurrency=1,
        shutdown_timeout_seconds=0.5,
    )
    await coordinator.start()
    await event_source.publish("test", {}, event_id="bad")
    await event_source.publish("test", {}, event_id="good")
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    await asyncio.sleep(0.02)
    await coordinator.stop()
    assert provider.starts == 1
    assert coordinator.errors == (("bad", "bad route"),)
    await runtime.stop()


@pytest.mark.asyncio
async def test_reactive_delivery_cursor_survives_restart_and_skips_completed_event() -> None:
    runtime, provider = await _runtime()
    source = MemoryEventSource()
    store = MemoryEventDeliveryStore()
    await source.publish("test", {}, event_id="event-1")
    executions: list[str] = []

    async def route(event: EventEnvelope) -> InvocationExecutionPlan:
        executions.append(event.event_id)
        return _plan(runtime, _request(f"restart-{event.event_id}"))

    first = ReactiveCoordinator(
        source,
        route,
        delivery_store=store,
        consumer_id="restart-consumer",
        shutdown_timeout_seconds=0.5,
    )
    await first.start()
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    await asyncio.sleep(0.02)
    await first.stop()
    assert await store.cursor("restart-consumer") == 1

    provider.started.clear()
    second = ReactiveCoordinator(
        source,
        route,
        delivery_store=store,
        consumer_id="restart-consumer",
        shutdown_timeout_seconds=0.5,
    )
    await second.start()
    await asyncio.sleep(0.05)
    await second.stop()
    assert executions == ["event-1"]
    record = await store.get("restart-consumer", "event-1")
    assert record is not None
    assert record.status is EventDeliveryStatus.SUCCEEDED
    await runtime.stop()


@pytest.mark.asyncio
async def test_jsonl_delivery_store_reopens_terminal_fact(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "delivery.jsonl")
    first = JsonlEventDeliveryStore(log)
    claimed = await first.claim("consumer", "event-1", 1)
    completed = await first.complete(claimed, status=EventDeliveryStatus.SUCCEEDED)
    assert completed.attempts == 1
    assert await first.cursor("consumer") == 1
    await log.close()

    reopened = JsonlEventDeliveryStore(JsonlEventLog(tmp_path / "delivery.jsonl"))
    restored = await reopened.get("consumer", "event-1")
    assert restored == completed
    assert await reopened.cursor("consumer") == 1
    assert await reopened.claim("consumer", "event-1", 1) == completed


@pytest.mark.asyncio
async def test_queue_retries_failed_invocation_but_not_reconciliation() -> None:
    runtime, provider = await _runtime(
        FakeAgentScenario(failure=FakeFailure("fake.failed", "transient"))
    )
    coordinator = QueueCoordinator(worker_count=1, shutdown_timeout_seconds=0.5)
    await coordinator.start()
    job = await coordinator.submit("job-retry", _plan(runtime, _request("queue-1")), max_attempts=2)
    result = await job.wait()
    assert result.status is QueueJobStatus.FAILED
    assert len(result.attempts) == 2
    assert provider.starts == 2
    await coordinator.stop()
    await runtime.stop()

    runtime, provider = await _runtime(
        FakeAgentScenario(
            failure=FakeFailure("fake.unknown", "unknown", reconciliation_required=True)
        )
    )
    coordinator = QueueCoordinator(worker_count=1, shutdown_timeout_seconds=0.5)
    await coordinator.start()
    job = await coordinator.submit(
        "job-uncertain", _plan(runtime, _request("queue-2")), max_attempts=3
    )
    result = await job.wait()
    assert result.status is QueueJobStatus.RECONCILIATION_REQUIRED
    assert len(result.attempts) == 1
    assert provider.starts == 1
    await coordinator.stop()
    await runtime.stop()


@pytest.mark.asyncio
async def test_queue_enforces_capacity_and_job_id_idempotency() -> None:
    runtime, provider = await _runtime(FakeAgentScenario(delay_seconds=0.1))
    coordinator = QueueCoordinator(capacity=1, worker_count=1, shutdown_timeout_seconds=0.5)
    await coordinator.start()
    first = await coordinator.submit("job-1", _plan(runtime, _request("queue-1")))
    await provider.started.wait()
    await coordinator.submit("job-2", _plan(runtime, _request("queue-2")))
    with pytest.raises(QueueCapacityExceeded):
        await coordinator.submit("job-3", _plan(runtime, _request("queue-3")))
    duplicate = await coordinator.submit("job-1", _plan(runtime, _request("queue-1")))
    assert duplicate.job_id == first.job_id
    with pytest.raises(CoordinatorConflict):
        await coordinator.submit("job-1", _plan(runtime, _request("different")))
    await coordinator.stop()
    await runtime.stop()


@pytest.mark.asyncio
async def test_queue_cancel_queued_job_and_replays_job_events() -> None:
    runtime, provider = await _runtime(FakeAgentScenario(delay_seconds=0.2))
    coordinator = QueueCoordinator(capacity=2, worker_count=1, shutdown_timeout_seconds=0.5)
    await coordinator.start()
    running = await coordinator.submit("job-running", _plan(runtime, _request("queue-running")))
    await provider.started.wait()
    queued = await coordinator.submit("job-queued", _plan(runtime, _request("queue-queued")))
    await queued.cancel("user cancelled")
    queued_result = await queued.wait()
    assert queued_result.status is QueueJobStatus.CANCELLED
    await running.cancel("cleanup")
    await running.wait()
    events = [event async for event in queued.events()]
    assert events[0].status is QueueJobStatus.QUEUED
    assert events[-1].status is QueueJobStatus.CANCELLED
    await coordinator.stop()
    await runtime.stop()
