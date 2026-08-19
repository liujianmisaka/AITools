from __future__ import annotations

import asyncio

import pytest
from misaka_coordinator_adapters import InvocationExecutionPlan
from misaka_coordinator_runtime import (
    CoordinatorConflict,
    CoordinatorStatus,
    DirectCoordinator,
    EventEnvelope,
    ExecutionStatus,
    MemoryEventSource,
    QueueCapacityExceeded,
    QueueCoordinator,
    QueueJobStatus,
    ReactiveCoordinator,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario, FakeFailure
from misaka_invocation_contracts import CompletionBoundary, InvocationRequest
from misaka_invocation_runtime import InvocationRuntime


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
