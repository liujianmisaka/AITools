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
    ProviderContractError,
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
    close_calls: int = 0
    reconcile_error: Exception | None = None
    provider_session_id: str | None = None
    provider_operation_id: str | None = None

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
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return ReconcileResult(ReconcileStatus.RUNNING, provider_operation_id="operation-1")

    async def close(self) -> None:
        self.close_calls += 1
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
        reconcile_error: Exception | None = None,
    ) -> None:
        self.starts = 0
        self.result_status = result_status
        self.features = features
        self.wait_gate = wait_gate
        self.emitted = emitted
        self.reconcile_error = reconcile_error
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
            reconcile_error=self.reconcile_error,
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


@dataclass(slots=True)
class _PreparedSession:
    request: InvocationRequest
    provider_session_id: str = "session-1"
    cleanup_error: str | None = None
    close_calls: int = 0

    async def close(self) -> str | None:
        self.close_calls += 1
        return self.cleanup_error


class _PreparedProvider(_Provider):
    def __init__(
        self,
        *,
        start_turn_gate: asyncio.Event | None = None,
        cleanup_error: str | None = None,
    ) -> None:
        super().__init__()
        self.prepare_calls = 0
        self.start_turn_calls = 0
        self.start_turn_gate = start_turn_gate
        self.cleanup_error = cleanup_error
        self.start_turn_entered = asyncio.Event()
        self.prepared: _PreparedSession | None = None

    async def start(self, request: InvocationRequest) -> ProviderHandle:
        del request
        raise AssertionError("runtime must use the prepared lifecycle")

    async def prepare_session(self, request: InvocationRequest) -> _PreparedSession:
        self.prepare_calls += 1
        self.prepared = _PreparedSession(request, cleanup_error=self.cleanup_error)
        return self.prepared

    async def start_turn(self, prepared: _PreparedSession) -> ProviderHandle:
        self.start_turn_calls += 1
        self.start_turn_entered.set()
        if self.start_turn_gate is not None:
            await self.start_turn_gate.wait()
        self.last_handle = _ProviderHandle(
            request=prepared.request,
            provider_session_id=prepared.provider_session_id,
            provider_operation_id="operation-1",
        )
        self.started.set()
        return self.last_handle


class _IncompletePreparedProvider(_Provider):
    async def prepare_session(self, request: InvocationRequest) -> _PreparedSession:
        return _PreparedSession(request)


class _RecoverableProvider(_Provider):
    def __init__(self, reconciliation: ReconcileResult) -> None:
        super().__init__()
        self.reconciliation = reconciliation
        self.reconcile_calls = 0

    async def reconcile_persisted(
        self,
        request: InvocationRequest,
        provider_execution: object,
    ) -> ReconcileResult:
        del request, provider_execution
        self.reconcile_calls += 1
        return self.reconciliation


def _request(
    invocation_id: str,
    key: str = "key",
    *,
    required_features: frozenset[CapabilityFeature] = frozenset(),
    attempt: int = 1,
) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key=key,
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        required_features=required_features,
        attempt=attempt,
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
    assert handle.activation_id == "inv-1:activation:1"
    assert snapshot.activation_id == handle.activation_id
    assert [event.sequence for event in snapshot.events] == list(range(1, 8))
    assert snapshot.events[-2].status is InvocationStatus.FINALIZING
    assert snapshot.events[-1].status is InvocationStatus.SUCCEEDED
    assert provider.starts == 1
    assert provider.last_handle is not None
    assert provider.last_handle.close_calls == 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_persists_prepared_session_before_starting_external_turn() -> None:
    start_turn_gate = asyncio.Event()
    provider = _PreparedProvider(start_turn_gate=start_turn_gate)
    runtime = InvocationRuntime()
    await runtime.register_provider("prepared", provider)

    handle = await runtime.submit(_request("inv-prepared"), provider_id="prepared")
    await provider.start_turn_entered.wait()
    starting = await handle.snapshot()

    assert starting.status is InvocationStatus.STARTING
    assert starting.provider_execution is not None
    assert starting.provider_execution.provider_id == "prepared"
    assert starting.provider_execution.provider_session_id == "session-1"
    assert starting.provider_execution.external_start_attempted
    assert provider.prepare_calls == 1
    assert provider.start_turn_calls == 1

    start_turn_gate.set()
    assert (await handle.wait()).status is InvocationStatus.SUCCEEDED
    terminal = await handle.snapshot()
    assert terminal.provider_execution is not None
    assert terminal.provider_execution.provider_operation_id == "operation-1"
    statuses = [event.status for event in terminal.events]
    assert InvocationStatus.RESOURCE_ACQUIRING in statuses
    assert InvocationStatus.PREPARED in statuses
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_closes_prepared_session_when_turn_start_is_cancelled() -> None:
    provider = _PreparedProvider(start_turn_gate=asyncio.Event())
    runtime = InvocationRuntime(cancellation_timeout_seconds=0.1)
    await runtime.register_provider("prepared", provider)
    handle = await runtime.submit(_request("inv-prepared-cancel"), provider_id="prepared")
    await provider.start_turn_entered.wait()

    await handle.cancel("cancel prepared turn start")
    result = await handle.wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert provider.prepared is not None
    assert provider.prepared.close_calls == 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_prepared_session_cleanup_failure_requires_reconciliation() -> None:
    provider = _PreparedProvider(
        start_turn_gate=asyncio.Event(),
        cleanup_error="client close failed",
    )
    runtime = InvocationRuntime(cancellation_timeout_seconds=0.1)
    await runtime.register_provider("prepared", provider)
    handle = await runtime.submit(_request("inv-prepared-cleanup"), provider_id="prepared")
    await provider.start_turn_entered.wait()

    await handle.cancel("cancel prepared turn start")
    result = await handle.wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "provider.prepared_cleanup_failed"
    assert result.error_message == "client close failed"
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_rejects_incomplete_prepared_provider_lifecycle() -> None:
    runtime = InvocationRuntime()

    with pytest.raises(ProviderContractError) as raised:
        await runtime.register_provider("incomplete", _IncompletePreparedProvider())

    assert raised.value.code == "provider.prepared_lifecycle_incomplete"


@pytest.mark.asyncio
async def test_runtime_recovers_registered_invocation_without_starting_twice() -> None:
    store = MemoryInvocationStore()
    request = _request("inv-recover-registered")
    await store.create(request)
    provider = _Provider()
    runtime = InvocationRuntime(store=store)
    await runtime.register_provider("fake", provider)

    handles = await runtime.recover()
    assert len(handles) == 1
    result = await handles[0].wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert provider.starts == 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_marks_running_external_work_unknown_when_provider_cannot_reconcile() -> None:
    store = MemoryInvocationStore()
    request = _request("inv-recover-unknown")
    await store.create(request)
    await store.append_event(
        request.invocation_id,
        InvocationStatus.PREFLIGHTING,
        {"provider_id": "fake", "provider_epoch": 1},
    )
    await store.append_event(
        request.invocation_id,
        InvocationStatus.STARTING,
        {
            "provider_id": "fake",
            "provider_epoch": 1,
            "external_start_attempted": True,
        },
    )
    await store.append_event(
        request.invocation_id,
        InvocationStatus.RUNNING,
        {
            "provider_id": "fake",
            "provider_epoch": 1,
            "external_start_attempted": True,
        },
    )
    provider = _Provider()
    runtime = InvocationRuntime(store=store)
    await runtime.register_provider("fake", provider)

    handles = await runtime.recover()
    result = await handles[0].wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "recovery.provider_reconcile_unavailable"
    assert provider.starts == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_uses_provider_recovery_for_a_persisted_terminal_result() -> None:
    store = MemoryInvocationStore()
    request = _request("inv-recover-terminal")
    await store.create(request)
    await store.append_event(
        request.invocation_id,
        InvocationStatus.PREFLIGHTING,
        {"provider_id": "recoverable", "provider_epoch": 1},
    )
    await store.append_event(
        request.invocation_id,
        InvocationStatus.STARTING,
        {
            "provider_id": "recoverable",
            "provider_epoch": 1,
            "external_start_attempted": True,
        },
    )
    await store.append_event(
        request.invocation_id,
        InvocationStatus.RUNNING,
        {
            "provider_id": "recoverable",
            "provider_epoch": 1,
            "external_start_attempted": True,
        },
    )
    provider = _RecoverableProvider(
        ReconcileResult(
            ReconcileStatus.SUCCEEDED,
            output={"answer": "recovered"},
            provider_operation_id="operation-1",
        )
    )
    runtime = InvocationRuntime(store=store)
    await runtime.register_provider("recoverable", provider)

    handles = await runtime.recover()
    result = await handles[0].wait()
    snapshot = await handles[0].snapshot()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "recovered"}
    assert provider.reconcile_calls == 1
    assert snapshot.events[-2].status is InvocationStatus.FINALIZING
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_does_not_recover_with_a_new_provider_epoch() -> None:
    store = MemoryInvocationStore()
    request = _request("inv-recover-epoch")
    await store.create(request)
    await store.append_event(
        request.invocation_id,
        InvocationStatus.PREFLIGHTING,
        {"provider_id": "fake", "provider_epoch": 1},
    )
    runtime = InvocationRuntime(store=store)
    first_dispose = await runtime.register_provider("fake", _Provider())
    await first_dispose()
    provider = _Provider()
    await runtime.register_provider("fake", provider)

    handles = await runtime.recover()
    result = await handles[0].wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "recovery.provider_epoch_mismatch"
    assert provider.starts == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_activation_identity_is_stable_for_duplicates_and_attempts() -> None:
    provider = _Provider()
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", provider)

    first = await runtime.submit(
        _request("inv-activation", key="activation-key"),
        provider_id="fake",
    )
    duplicate = await runtime.submit(
        _request("other-id", key="activation-key"),
        provider_id="fake",
    )
    await first.wait()
    await duplicate.wait()

    assert first.activation_id == duplicate.activation_id == "inv-activation:activation:1"

    attempt_two_request = _request(
        "inv-activation:attempt:2",
        key="activation-key:attempt:2",
        attempt=2,
    )
    attempt_two = await runtime.submit(attempt_two_request, provider_id="fake")
    await attempt_two.wait()
    assert attempt_two.activation_id == "inv-activation:attempt:2:activation:2"
    assert attempt_two.activation_id != first.activation_id
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
async def test_runtime_reconcile_preserves_terminal_result_details() -> None:
    provider = _Provider()
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", provider)
    handle = await runtime.submit(_request("inv-terminal-reconcile"), provider_id="fake")
    result = await handle.wait()

    reconciled = await handle.reconcile()

    assert result.status is InvocationStatus.SUCCEEDED
    assert reconciled.status is ReconcileStatus.SUCCEEDED
    assert reconciled.output == result.output
    assert reconciled.error_code is None
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_reconcile_provider_error_is_unreachable() -> None:
    provider = _Provider(reconcile_error=RuntimeError("provider unavailable"))
    wait_gate = asyncio.Event()
    provider.wait_gate = wait_gate
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", provider)
    handle = await runtime.submit(_request("inv-reconcile-error"), provider_id="fake")
    await provider.started.wait()

    reconciled = await handle.reconcile()

    assert reconciled.status is ReconcileStatus.UNREACHABLE
    assert reconciled.error_code == "provider.reconcile_failed"
    await handle.cancel("test cleanup")
    await handle.wait()
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_cancel_uses_first_reason_when_requested_repeatedly() -> None:
    wait_gate = asyncio.Event()
    provider = _Provider(wait_gate=wait_gate)
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", provider)
    handle = await runtime.submit(_request("inv-cancel-idempotent"), provider_id="fake")
    await provider.started.wait()

    await handle.cancel("first reason")
    await handle.cancel("second reason")
    result = await handle.wait()

    assert result.status is InvocationStatus.CANCELLED
    assert provider.last_handle is not None
    assert provider.last_handle.cancel_reasons == ["first reason"]
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
async def test_memory_store_fences_provider_binding_identity() -> None:
    store = MemoryInvocationStore()
    await store.create(_request("inv-provider-fence"))
    await store.append_event(
        "inv-provider-fence",
        InvocationStatus.PREFLIGHTING,
        {"provider_id": "provider-a", "provider_epoch": 1},
    )

    with pytest.raises(InvocationError) as raised:
        await store.append_event(
            "inv-provider-fence",
            InvocationStatus.STARTING,
            {
                "provider_id": "provider-b",
                "provider_epoch": 1,
                "external_start_attempted": True,
            },
        )

    assert raised.value.code == "invocation.provider_binding_conflict"


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
