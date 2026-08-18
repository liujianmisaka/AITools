from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from misaka_invocation_contracts import (
    CapabilityDescriptor,
    CapabilityFeature,
    CapabilityOperation,
    CompletionBoundary,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ReconcileResult,
    ReconcileStatus,
)
from misaka_invocation_runtime import (
    IdempotencyConflict,
    InvocationError,
    InvocationRuntime,
    MemoryInvocationStore,
    ProviderExecutionError,
    ProviderHandle,
)


@dataclass
class _ProviderHandle:
    request: InvocationRequest
    result_status: InvocationStatus = InvocationStatus.SUCCEEDED
    emitted: tuple[InvocationEvent, ...] = ()
    wait_gate: asyncio.Event | None = None
    cancelled: bool = False
    cancel_reasons: list[str] = field(default_factory=list)

    async def events(self) -> AsyncIterator[InvocationEvent]:
        for event in self.emitted:
            await asyncio.sleep(0)
            yield event

    async def wait(self) -> InvocationResult:
        if self.wait_gate is not None:
            await self.wait_gate.wait()
        status = InvocationStatus.CANCELLED if self.cancelled else self.result_status
        return InvocationResult(
            invocation_id=self.request.invocation_id,
            status=status,
            output={"answer": "ok"} if status is InvocationStatus.SUCCEEDED else None,
            error_code="invocation.cancelled" if status is InvocationStatus.CANCELLED else None,
            error_message="cancelled by test" if status is InvocationStatus.CANCELLED else None,
        )

    async def cancel(self, reason: str) -> None:
        self.cancelled = True
        self.cancel_reasons.append(reason)
        if self.wait_gate is not None:
            self.wait_gate.set()

    async def reconcile(self) -> ReconcileResult:
        return ReconcileResult(ReconcileStatus.RUNNING, provider_operation_id="operation-1")

    async def close(self) -> None:
        self.cancelled = True
        if self.wait_gate is not None:
            self.wait_gate.set()


class _Provider:
    def __init__(
        self,
        *,
        result_status: InvocationStatus = InvocationStatus.SUCCEEDED,
        features: frozenset[CapabilityFeature] = frozenset(),
        wait_gate: asyncio.Event | None = None,
        emitted: tuple[InvocationEvent, ...] = (),
    ) -> None:
        self.starts = 0
        self.result_status = result_status
        self.features = features
        self.wait_gate = wait_gate
        self.emitted = emitted
        self.started = asyncio.Event()
        self.last_handle: _ProviderHandle | None = None

    async def describe(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id="agent.invocation",
            version="1.0.0",
            operations=(CapabilityOperation(name="invoke"),),
            features=self.features,
        )

    async def start(self, request: InvocationRequest) -> ProviderHandle:
        self.starts += 1
        self.last_handle = _ProviderHandle(
            request=request,
            result_status=self.result_status,
            wait_gate=self.wait_gate,
            emitted=self.emitted,
        )
        self.started.set()
        return self.last_handle


class _UncertainProvider(_Provider):
    async def start(self, request: InvocationRequest) -> ProviderHandle:
        raise ProviderExecutionError(
            "provider.start_unknown",
            "provider start outcome is unknown",
            reconciliation_required=True,
        )


class _UnexpectedStartFailureProvider(_Provider):
    async def start(self, request: InvocationRequest) -> ProviderHandle:
        raise RuntimeError("unexpected provider failure")


class _HangingHandle:
    def __init__(self, request: InvocationRequest, *, close_hangs: bool = False) -> None:
        self.request = request
        self.close_hangs = close_hangs
        self.closed = asyncio.Event()
        self.close_calls = 0

    async def events(self) -> AsyncIterator[InvocationEvent]:
        await self.closed.wait()
        if False:
            yield InvocationEvent(
                invocation_id=self.request.invocation_id,
                sequence=1,
                status=InvocationStatus.RUNNING,
            )

    async def wait(self) -> InvocationResult:
        await self.closed.wait()
        return InvocationResult(
            invocation_id=self.request.invocation_id,
            status=InvocationStatus.RECONCILIATION_REQUIRED,
            error_code="provider.force_closed",
            error_message="provider was force-closed",
        )

    async def cancel(self, reason: str) -> None:
        del reason
        await asyncio.Event().wait()

    async def reconcile(self) -> ReconcileResult:
        return ReconcileResult(ReconcileStatus.RUNNING)

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_hangs:
            await asyncio.Event().wait()
        self.closed.set()


class _HangingProvider(_Provider):
    def __init__(self, *, close_hangs: bool = False) -> None:
        super().__init__()
        self.close_hangs = close_hangs
        self.hanging_handle: _HangingHandle | None = None

    async def start(self, request: InvocationRequest) -> ProviderHandle:
        self.starts += 1
        self.hanging_handle = _HangingHandle(request, close_hangs=self.close_hangs)
        self.started.set()
        return self.hanging_handle


class _HangingStartProvider(_Provider):
    async def start(self, request: InvocationRequest) -> ProviderHandle:
        del request
        self.starts += 1
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _request(
    invocation_id: str,
    key: str = "key",
    *,
    required_features: frozenset[CapabilityFeature] = frozenset(),
) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key=key,
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        required_features=required_features,
    )


@pytest.mark.asyncio
async def test_runtime_executes_provider_and_normalizes_events() -> None:
    provider = _Provider(
        emitted=(
            InvocationEvent(
                invocation_id="inv-1",
                sequence=1,
                status=InvocationStatus.RUNNING,
                payload={"phase": "provider"},
            ),
        )
    )
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", provider)

    handle = await runtime.submit(_request("inv-1"), provider_id="fake")
    result = await handle.wait()
    snapshot = await handle.snapshot()

    assert result.status is InvocationStatus.SUCCEEDED
    assert [event.sequence for event in snapshot.events] == list(range(1, 8))
    assert snapshot.events[-2].status is InvocationStatus.FINALIZING
    assert snapshot.events[-1].status is InvocationStatus.SUCCEEDED
    assert provider.starts == 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_duplicate_idempotency_does_not_start_provider_twice() -> None:
    provider = _Provider()
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", provider)

    first = await runtime.submit(_request("inv-1", key="same"), provider_id="fake")
    second = await runtime.submit(_request("inv-2", key="same"), provider_id="fake")
    assert first.invocation_id == second.invocation_id == "inv-1"
    await first.wait()
    await second.wait()
    assert provider.starts == 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_rejects_idempotency_conflict() -> None:
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", _Provider())
    await runtime.submit(_request("inv-1", key="same"), provider_id="fake")
    with pytest.raises(IdempotencyConflict):
        await runtime.submit(
            InvocationRequest(
                invocation_id="inv-2",
                capability_id="agent.invocation",
                operation="invoke",
                input={"prompt": "different"},
                idempotency_key="same",
                completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
            ),
            provider_id="fake",
        )
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_rejects_missing_provider_and_unsupported_feature() -> None:
    runtime = InvocationRuntime()
    missing_result = await (await runtime.submit(_request("inv-missing"))).wait()
    assert missing_result.status is InvocationStatus.REJECTED
    assert missing_result.error_code == "capability.unavailable"

    await runtime.register_provider("fake", _Provider())
    unsupported_result = await (
        await runtime.submit(
            _request(
                "inv-unsupported",
                key="unsupported",
                required_features=frozenset({CapabilityFeature.STREAMING}),
            ),
            provider_id="fake",
        )
    ).wait()
    assert unsupported_result.status is InvocationStatus.REJECTED
    assert unsupported_result.error_code == "capability.unsupported"
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_marks_unknown_provider_start_as_reconciliation_required() -> None:
    runtime = InvocationRuntime()
    await runtime.register_provider("uncertain", _UncertainProvider())
    result = await (await runtime.submit(_request("inv-1"), provider_id="uncertain")).wait()
    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "provider.start_unknown"
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_treats_unexpected_start_failure_as_uncertain() -> None:
    runtime = InvocationRuntime()
    await runtime.register_provider("broken", _UnexpectedStartFailureProvider())
    result = await (await runtime.submit(_request("inv-1"), provider_id="broken")).wait()
    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "RuntimeError"
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_rejects_non_contiguous_provider_event_sequence() -> None:
    provider = _Provider(
        emitted=(
            InvocationEvent(
                invocation_id="inv-1",
                sequence=2,
                status=InvocationStatus.RUNNING,
            ),
        )
    )
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", provider)

    result = await (await runtime.submit(_request("inv-1"), provider_id="fake")).wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "provider.event_sequence_invalid"
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_cancel_waits_for_provider_terminal_result() -> None:
    wait_gate = asyncio.Event()
    provider = _Provider(wait_gate=wait_gate)
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", provider)
    handle = await runtime.submit(_request("inv-1"), provider_id="fake")
    await provider.started.wait()

    await handle.cancel("user requested cancellation")
    result = await handle.wait()
    snapshot = await handle.snapshot()

    assert result.status is InvocationStatus.CANCELLED
    assert provider.last_handle is not None
    assert provider.last_handle.cancel_reasons == ["user requested cancellation"]
    assert InvocationStatus.STOPPING in [event.status for event in snapshot.events]
    assert snapshot.events[-2].status is InvocationStatus.FINALIZING
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_reconcile_uses_active_provider_handle() -> None:
    wait_gate = asyncio.Event()
    provider = _Provider(wait_gate=wait_gate)
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", provider)
    handle = await runtime.submit(_request("inv-1"), provider_id="fake")
    await provider.started.wait()

    reconciled = await handle.reconcile()

    assert reconciled.status is ReconcileStatus.RUNNING
    await handle.cancel("test cleanup")
    await handle.wait()
    await runtime.stop()


@pytest.mark.asyncio
async def test_memory_store_rejects_events_after_terminal_result() -> None:
    store = MemoryInvocationStore()
    request = _request("inv-1")
    await store.create(request)
    await store.finalize(InvocationResult(invocation_id="inv-1", status=InvocationStatus.CANCELLED))

    with pytest.raises(InvocationError) as raised:
        await store.append_event("inv-1", InvocationStatus.RUNNING, {})

    assert raised.value.code == "invocation.already_terminal"


@pytest.mark.asyncio
async def test_cancel_force_closes_unresponsive_provider() -> None:
    provider = _HangingProvider(close_hangs=True)
    runtime = InvocationRuntime(cancellation_timeout_seconds=0.01)
    await runtime.register_provider("hanging", provider)
    handle = await runtime.submit(_request("inv-hanging"), provider_id="hanging")
    await provider.started.wait()

    await handle.cancel("bounded cancellation test")
    result = await handle.wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "invocation.cancel_timeout"
    assert provider.hanging_handle is not None
    assert provider.hanging_handle.close_calls >= 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_stop_has_a_hard_deadline() -> None:
    provider = _HangingProvider()
    runtime = InvocationRuntime(
        cancellation_timeout_seconds=1.0,
        shutdown_timeout_seconds=0.01,
    )
    await runtime.register_provider("hanging", provider)
    handle = await runtime.submit(_request("inv-stop-timeout"), provider_id="hanging")
    await provider.started.wait()

    await runtime.stop()
    result = await handle.wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "invocation.shutdown_timeout"
    assert provider.hanging_handle is not None
    assert provider.hanging_handle.close_calls >= 1


@pytest.mark.asyncio
async def test_provider_start_has_a_hard_deadline() -> None:
    provider = _HangingStartProvider()
    runtime = InvocationRuntime(provider_start_timeout_seconds=0.01)
    await runtime.register_provider("hanging-start", provider)

    result = await (
        await runtime.submit(_request("inv-start-timeout"), provider_id="hanging-start")
    ).wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "provider.start_timeout"
    await runtime.stop()


@pytest.mark.asyncio
async def test_cancel_interrupts_provider_start() -> None:
    provider = _HangingStartProvider()
    runtime = InvocationRuntime(
        provider_start_timeout_seconds=1.0,
        cancellation_timeout_seconds=0.01,
    )
    await runtime.register_provider("hanging-start", provider)
    handle = await runtime.submit(_request("inv-cancel-start"), provider_id="hanging-start")
    await provider.started.wait()

    await handle.cancel("cancel during provider start")
    result = await handle.wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "invocation.execution_aborted"
    await runtime.stop()
