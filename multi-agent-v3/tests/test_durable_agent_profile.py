from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from misaka_agent_host_profile import create_fake_agent_host
from misaka_coordinator_temporal import TemporalInvocationInput
from misaka_durable_agent import DurableAgentConfig, DurableAgentProfile
from misaka_fake_agent import FakeAgentScenario
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
)
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableEvent
from misaka_persistence_postgres import PostgresDurableStore
from temporalio.client import Client


class _MemoryAuditStore:
    def __init__(self, *, fail_event_type: str | None = None) -> None:
        self.started = False
        self.closed = False
        self.fail_event_type = fail_event_type
        self.events: list[DurableEvent] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def append(
        self,
        stream_id: str,
        event_id: str,
        event_type: str,
        payload: JsonObject,
        *,
        occurred_at: datetime | None = None,
    ) -> DurableEvent:
        if event_type == self.fail_event_type:
            raise RuntimeError(f"audit unavailable for {event_type}")
        existing = next(
            (
                event
                for event in self.events
                if event.stream_id == stream_id and event.event_id == event_id
            ),
            None,
        )
        if existing is not None:
            return existing
        event = DurableEvent(
            stream_id=stream_id,
            sequence=sum(item.stream_id == stream_id for item in self.events) + 1,
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            occurred_at=occurred_at or datetime.now(UTC),
        )
        self.events.append(event)
        return event

    async def read(self, stream_id: str, *, start_sequence: int = 1) -> tuple[DurableEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.stream_id == stream_id and event.sequence >= start_sequence
        )


class _FakeExecution:
    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        self.run_id = "run-1"
        self.cancelled = False

    async def wait(self) -> InvocationResult:
        return InvocationResult(
            invocation_id="inv-1",
            status=InvocationStatus.CANCELLED if self.cancelled else InvocationStatus.SUCCEEDED,
            output=None if self.cancelled else {"answer": "durable-ok"},
            error_code="cancelled" if self.cancelled else None,
        )

    async def cancel(self) -> None:
        self.cancelled = True


class _FakeCoordinator:
    def __init__(self) -> None:
        self.inputs: list[tuple[str, TemporalInvocationInput]] = []
        self.execution: _FakeExecution | None = None

    async def start(self, workflow_id: str, input: TemporalInvocationInput) -> _FakeExecution:
        self.inputs.append((workflow_id, input))
        self.execution = _FakeExecution(workflow_id)
        return self.execution


class _FakeWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._stop = asyncio.Event()
        self.is_running = False

    async def run(self) -> None:
        self.is_running = True
        self.started.set()
        try:
            await self._stop.wait()
        finally:
            self.is_running = False

    async def shutdown(self) -> None:
        self._stop.set()


def _request(invocation_id: str = "inv-1") -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "run durable"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        model="fake/model",
        effort="high",
    )


@pytest.mark.asyncio
async def test_durable_profile_uses_temporal_execution_and_postgres_audit_port() -> None:
    store = _MemoryAuditStore()
    coordinator = _FakeCoordinator()
    worker = _FakeWorker()
    profile = DurableAgentProfile(
        create_fake_agent_host(),
        store,
        coordinator,
        worker,
        config=DurableAgentConfig(task_queue="test-queue"),
    )

    await profile.start()
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    handle = await profile.submit(_request(), workflow_id="workflow-1", provider_id="fake-agent")
    result = await handle.wait()
    repeated = await handle.wait()
    await profile.stop()

    assert result.status is InvocationStatus.SUCCEEDED
    assert repeated == result
    assert coordinator.inputs[0][0] == "workflow-1"
    assert [event.event_type for event in store.events] == [
        "durable.invocation.accepted",
        "durable.invocation.started",
        "durable.invocation.completed",
    ]
    assert store.started and store.closed
    assert not profile.agent_host.status.value == "active"


@pytest.mark.asyncio
async def test_durable_profile_cancel_is_audited_and_delegated() -> None:
    store = _MemoryAuditStore()
    coordinator = _FakeCoordinator()
    worker = _FakeWorker()
    profile = DurableAgentProfile(create_fake_agent_host(), store, coordinator, worker)
    await profile.start()
    handle = await profile.submit(_request())

    await handle.cancel("user requested cancellation")
    result = await handle.wait()
    await profile.stop()

    assert result.status is InvocationStatus.CANCELLED
    assert coordinator.execution is not None and coordinator.execution.cancelled
    assert "durable.invocation.cancel_requested" in [event.event_type for event in store.events]


@pytest.mark.asyncio
async def test_durable_profile_rejects_use_before_start() -> None:
    profile = DurableAgentProfile(
        create_fake_agent_host(),
        _MemoryAuditStore(),
        _FakeCoordinator(),
        _FakeWorker(),
    )
    with pytest.raises(RuntimeError, match="must be started"):
        await profile.submit(_request())


@pytest.mark.asyncio
async def test_durable_profile_audit_failure_does_not_mask_temporal_result() -> None:
    store = _MemoryAuditStore(fail_event_type="durable.invocation.started")
    coordinator = _FakeCoordinator()
    worker = _FakeWorker()
    profile = DurableAgentProfile(create_fake_agent_host(), store, coordinator, worker)
    await profile.start()
    handle = await profile.submit(_request())

    result = await handle.wait()
    await profile.stop()

    assert result.status is InvocationStatus.SUCCEEDED
    assert handle.audit_errors == ("started: audit unavailable for durable.invocation.started",)


@pytest.mark.asyncio
async def test_durable_profile_real_temporal_and_postgres_integration() -> None:
    dsn = os.environ.get("MULTI_AGENT_V3_POSTGRES_DSN")
    target = os.environ.get("MULTI_AGENT_V3_TEMPORAL_TARGET")
    if dsn is None or target is None:
        pytest.skip("PostgreSQL and Temporal integration targets are not configured")

    suffix = uuid.uuid4().hex
    invocation_id = f"durable-{suffix}"
    client = await Client.connect(target)
    store = PostgresDurableStore(dsn)
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "durable-real-ok"}))
    profile = DurableAgentProfile.from_temporal(
        host,
        store,
        client,
        config=DurableAgentConfig(task_queue=f"durable-agent-{suffix}"),
    )
    try:
        await profile.start()
        handle = await profile.submit(
            _request(invocation_id),
            workflow_id=f"workflow-{suffix}",
            provider_id="fake-agent",
        )
        result = await handle.wait()
        events = await store.read(f"durable-agent:{invocation_id}")

        assert result.status is InvocationStatus.SUCCEEDED
        assert result.output == {"answer": "durable-real-ok"}
        assert [event.event_type for event in events] == [
            "durable.invocation.accepted",
            "durable.invocation.started",
            "durable.invocation.completed",
        ]
    finally:
        await profile.stop()
