from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from misaka_capability_catalog import (
    CapabilityCatalog,
    MemoryCapabilityCatalog,
    ProviderRegistration,
    RegistrationHandle,
    matches_json_schema,
)
from misaka_invocation_contracts import (
    CapabilityDescriptor,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ModelCatalog,
    ModelDescriptor,
    ReconcileResult,
    ReconcileStatus,
)
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import JsonObject

from misaka_invocation_runtime.errors import (
    CapabilityUnavailable,
    InvocationError,
    InvocationRejected,
    ProviderContractError,
    ProviderExecutionError,
)
from misaka_invocation_runtime.provider import (
    InvocationGuard,
    InvocationProvider,
    PersistedProviderRecovery,
    PreparedProviderSession,
    ProviderHandle,
)
from misaka_invocation_runtime.store import (
    InvocationSnapshot,
    InvocationStore,
    MemoryInvocationStore,
    ownership_payload,
)


@dataclass(frozen=True, slots=True)
class _RegisteredProvider:
    provider_id: str
    provider: InvocationProvider
    descriptor: CapabilityDescriptor
    registration: ProviderRegistration
    registration_handle: RegistrationHandle
    prepared_lifecycle: _PreparedLifecycle | None
    persisted_recovery: PersistedProviderRecovery | None


@dataclass(frozen=True, slots=True)
class _PreparedLifecycle:
    prepare_session: Callable[[InvocationRequest], Awaitable[PreparedProviderSession]]
    start_turn: Callable[[PreparedProviderSession], Awaitable[ProviderHandle]]


class RuntimeInvocationHandle:
    def __init__(self, runtime: InvocationRuntime, invocation_id: str, activation_id: str) -> None:
        self._runtime = runtime
        self.invocation_id = invocation_id
        self.activation_id = activation_id

    async def events(self, *, start_sequence: int = 1) -> AsyncIterator[InvocationEvent]:
        async for event in self._runtime.store.events(
            self.invocation_id,
            start_sequence=start_sequence,
        ):
            yield event

    async def wait(self) -> InvocationResult:
        return await self._runtime.store.wait_terminal(self.invocation_id)

    async def cancel(self, reason: str) -> None:
        await self._runtime.cancel(self.invocation_id, reason)

    async def reconcile(self) -> ReconcileResult:
        return await self._runtime.reconcile(self.invocation_id)

    async def snapshot(self) -> InvocationSnapshot:
        return await self._runtime.store.snapshot(self.invocation_id)


class InvocationRuntime:
    def __init__(
        self,
        *,
        store: InvocationStore | None = None,
        capability_catalog: CapabilityCatalog | None = None,
        provider_start_timeout_seconds: float = 60.0,
        cancellation_timeout_seconds: float = 10.0,
        shutdown_timeout_seconds: float = 15.0,
    ) -> None:
        if (
            provider_start_timeout_seconds <= 0
            or cancellation_timeout_seconds <= 0
            or shutdown_timeout_seconds <= 0
        ):
            raise ValueError("runtime timeout values must be positive")
        self.store = store or MemoryInvocationStore()
        self.capability_catalog = capability_catalog or MemoryCapabilityCatalog()
        self.provider_start_timeout_seconds = provider_start_timeout_seconds
        self.cancellation_timeout_seconds = cancellation_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._providers: dict[str, _RegisteredProvider] = {}
        self._guards: list[InvocationGuard] = []
        self._active_handles: dict[str, ProviderHandle] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._starting: set[str] = set()
        self._cancel_requests: dict[str, str] = {}
        self._stopping = False

    async def register_provider(
        self,
        provider_id: str,
        provider: InvocationProvider,
        *,
        owner_id: str = "invocation-runtime",
        scope_id: str = "runtime",
    ) -> AsyncDisposer:
        if self._stopping:
            raise InvocationError("runtime.stopping", "runtime is stopping")
        if not provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if provider_id in self._providers:
            raise InvocationError(
                "provider.duplicate", f"provider {provider_id} is already registered"
            )
        descriptor = await provider.describe()
        prepared_lifecycle = _prepared_lifecycle(provider)
        persisted_recovery = _persisted_recovery(provider)
        registration_handle = self.capability_catalog.register(
            provider_id,
            descriptor,
            owner_id=owner_id,
            scope_id=scope_id,
        )
        registered = _RegisteredProvider(
            provider_id,
            provider,
            descriptor,
            registration_handle.registration,
            registration_handle,
            prepared_lifecycle,
            persisted_recovery,
        )
        self._providers[provider_id] = registered

        async def dispose() -> None:
            await self.unregister_provider(
                provider_id,
                registration_id=registration_handle.registration.registration_id,
            )

        return dispose

    async def unregister_provider(
        self, provider_id: str, *, registration_id: str | None = None
    ) -> None:
        registered = self._providers.get(provider_id)
        if registered is None:
            return
        if (
            registration_id is not None
            and registered.registration.registration_id != registration_id
        ):
            return
        del self._providers[provider_id]
        await registered.registration_handle.dispose()

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(item.descriptor for item in self.capability_catalog.snapshot())

    async def model_catalogs(self, *, include_hidden: bool = False) -> tuple[ModelCatalog, ...]:
        """Read provider model directories without starting an invocation."""
        catalogs: list[ModelCatalog] = []
        for registered in self._providers.values():
            catalog_method = getattr(registered.provider, "model_catalog", None)
            if not callable(catalog_method):
                continue
            typed_catalog_method = cast(
                Callable[..., Awaitable[tuple[ModelDescriptor, ...]]],
                catalog_method,
            )
            models = await typed_catalog_method(include_hidden=include_hidden)
            catalogs.append(
                ModelCatalog(
                    provider_id=registered.provider_id,
                    models=tuple(models),
                )
            )
        return tuple(catalogs)

    def add_guard(self, guard: InvocationGuard) -> Callable[[], None]:
        if self._stopping:
            raise InvocationError("runtime.stopping", "runtime is stopping")
        if guard in self._guards:
            raise InvocationError(
                "runtime.guard_duplicate", "invocation guard is already registered"
            )
        self._guards.append(guard)
        removed = False

        def remove() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            if guard in self._guards:
                self._guards.remove(guard)

        return remove

    async def submit(
        self,
        request: InvocationRequest,
        *,
        provider_id: str | None = None,
    ) -> RuntimeInvocationHandle:
        if self._stopping:
            raise InvocationError("runtime.stopping", "runtime is stopping")
        snapshot, created = await self.store.create(request)
        handle = RuntimeInvocationHandle(
            self,
            snapshot.request.invocation_id,
            snapshot.activation_id,
        )
        if not created:
            return handle
        try:
            provider = self._select_provider(request.capability_id, provider_id)
        except CapabilityUnavailable as exc:
            await self._finalize_error(
                request,
                exc,
                reconciliation_required=False,
                rejected=True,
            )
            return handle
        self._schedule(request, provider)
        return handle

    async def recover(self) -> tuple[RuntimeInvocationHandle, ...]:
        """Resume safe pre-start records and reconcile uncertain external work."""
        if self._stopping:
            raise InvocationError("runtime.stopping", "runtime is stopping")
        recovered: list[RuntimeInvocationHandle] = []
        for snapshot in await self.store.list():
            if snapshot.result is not None or snapshot.request.invocation_id in self._tasks:
                continue
            handle = RuntimeInvocationHandle(
                self,
                snapshot.request.invocation_id,
                snapshot.activation_id,
            )
            recovered.append(handle)
            provider_id = (
                snapshot.provider_execution.provider_id
                if snapshot.provider_execution is not None
                else None
            )
            try:
                provider = self._select_provider(snapshot.request.capability_id, provider_id)
            except CapabilityUnavailable as exc:
                await self._finalize_recovery_error(snapshot, exc)
                continue
            if (
                snapshot.provider_execution is not None
                and snapshot.provider_execution.provider_epoch != provider.registration.epoch
            ):
                await self._finalize_recovery_error(
                    snapshot,
                    InvocationError(
                        "recovery.provider_epoch_mismatch",
                        "persisted provider binding epoch is no longer active",
                    ),
                )
                continue
            if snapshot.status in {
                InvocationStatus.REGISTERED,
                InvocationStatus.PREFLIGHTING,
            } and (
                snapshot.provider_execution is None
                or not snapshot.provider_execution.external_start_attempted
            ):
                self._schedule(snapshot.request, provider, resume=True)
                continue
            await self._reconcile_persisted(snapshot, provider)
        return tuple(recovered)

    def _schedule(
        self,
        request: InvocationRequest,
        provider: _RegisteredProvider,
        *,
        resume: bool = False,
    ) -> None:
        task = asyncio.create_task(self._execute(request, provider, resume=resume))
        self._tasks[request.invocation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(request.invocation_id, None))

    async def cancel(self, invocation_id: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        snapshot = await self.store.snapshot(invocation_id)
        if snapshot.result is not None:
            return
        if invocation_id in self._cancel_requests:
            return
        self._cancel_requests[invocation_id] = reason
        provider_handle = self._active_handles.get(invocation_id)
        if provider_handle is None:
            if invocation_id not in self._tasks:
                await self.store.finalize(
                    InvocationResult(
                        invocation_id=invocation_id,
                        status=InvocationStatus.RECONCILIATION_REQUIRED,
                        error_code="invocation.cancel_unknown",
                        error_message=f"provider handle is unavailable: {reason}",
                    )
                )
            elif invocation_id in self._starting:
                if snapshot.status is InvocationStatus.PREPARED:
                    task = self._tasks.get(invocation_id)
                    if task is not None:
                        try:
                            async with asyncio.timeout(self.cancellation_timeout_seconds):
                                await self.store.wait_terminal(invocation_id)
                        except TimeoutError:
                            task.cancel()
                            try:
                                async with asyncio.timeout(self.cancellation_timeout_seconds):
                                    await asyncio.gather(task, return_exceptions=True)
                            except TimeoutError:
                                await self._finalize_unproven_termination(
                                    invocation_id,
                                    error_code="invocation.cancel_timeout",
                                    reason=(
                                        "prepared provider session did not stop before the deadline"
                                    ),
                                )
                    return
                if snapshot.status is InvocationStatus.STARTING:
                    await self._append_stopping(invocation_id, reason)
                task = self._tasks.get(invocation_id)
                if task is not None:
                    task.cancel()
                    try:
                        async with asyncio.timeout(self.cancellation_timeout_seconds):
                            await asyncio.gather(task, return_exceptions=True)
                    except TimeoutError:
                        await self._finalize_unproven_termination(
                            invocation_id,
                            error_code="invocation.cancel_timeout",
                            reason="provider start did not stop before the cancellation deadline",
                        )
            return
        await self._append_stopping(invocation_id, reason)
        try:
            async with asyncio.timeout(self.cancellation_timeout_seconds):
                await provider_handle.cancel(reason)
                await self.store.wait_terminal(invocation_id)
        except TimeoutError:
            await self._force_close(
                invocation_id,
                provider_handle,
                "provider did not confirm cancellation before the deadline",
                error_code="invocation.cancel_timeout",
            )

    async def reconcile(self, invocation_id: str) -> ReconcileResult:
        snapshot = await self.store.snapshot(invocation_id)
        if snapshot.result is not None:
            return _reconcile_from_terminal(snapshot.result)
        provider_handle = self._active_handles.get(invocation_id)
        if provider_handle is None:
            return ReconcileResult(
                ReconcileStatus.UNREACHABLE,
                message=(
                    "no provider handle is attached to the invocation; "
                    f"last status was {snapshot.status.value}"
                ),
                error_code="invocation.handle_unavailable",
            )
        try:
            return await provider_handle.reconcile()
        except Exception as exc:
            return ReconcileResult(
                ReconcileStatus.UNREACHABLE,
                message=f"provider reconciliation failed: {exc}",
                error_code="provider.reconcile_failed",
                error_message=str(exc),
            )

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        invocation_ids = tuple(self._tasks)
        for invocation_id in invocation_ids:
            self._cancel_requests.setdefault(invocation_id, "invocation runtime stopping")
        try:
            async with asyncio.timeout(self.shutdown_timeout_seconds):
                await asyncio.gather(
                    *(
                        self.cancel(invocation_id, "invocation runtime stopping")
                        for invocation_id in invocation_ids
                    ),
                    return_exceptions=True,
                )
                tasks = tuple(self._tasks.values())
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            handles = tuple(self._active_handles.items())
            await asyncio.gather(
                *(
                    self._force_close(
                        invocation_id,
                        provider_handle,
                        "invocation runtime shutdown deadline expired",
                        error_code="invocation.shutdown_timeout",
                    )
                    for invocation_id, provider_handle in handles
                ),
                return_exceptions=True,
            )
            tasks = tuple(self._tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for invocation_id in invocation_ids:
                await self._finalize_unproven_termination(
                    invocation_id,
                    error_code="invocation.shutdown_timeout",
                    reason="invocation runtime shutdown deadline expired",
                )
        for provider_id in tuple(self._providers):
            await self.unregister_provider(provider_id)
        self._active_handles.clear()
        self._starting.clear()
        self._cancel_requests.clear()

    def _select_provider(self, capability_id: str, provider_id: str | None) -> _RegisteredProvider:
        candidates = [
            item
            for item in self._providers.values()
            if item.descriptor.capability_id == capability_id
        ]
        if provider_id is not None:
            candidates = [item for item in candidates if item.provider_id == provider_id]
        if not candidates:
            raise CapabilityUnavailable(
                "capability.unavailable",
                f"no provider is registered for capability {capability_id}",
            )
        if len(candidates) > 1 and provider_id is None:
            raise CapabilityUnavailable(
                "capability.ambiguous",
                f"multiple providers are registered for capability {capability_id}",
            )
        return candidates[0]

    async def _execute(
        self,
        request: InvocationRequest,
        registered: _RegisteredProvider,
        *,
        resume: bool = False,
    ) -> None:
        provider_handle: ProviderHandle | None = None
        prepared_session: PreparedProviderSession | None = None
        prepared_handed_off = False
        start_attempted = False
        result: InvocationResult | None = None
        try:
            operation = next(
                (
                    candidate
                    for candidate in registered.descriptor.operations
                    if candidate.name == request.operation
                ),
                None,
            )
            if operation is None:
                raise CapabilityUnavailable(
                    "capability.operation_unavailable",
                    f"operation {request.operation} is not supported",
                )
            if operation.input_schema and not matches_json_schema(
                request.input, operation.input_schema
            ):
                raise InvocationRejected(
                    "capability.input_schema_invalid",
                    f"input does not satisfy capability operation {request.operation} schema",
                )
            missing_features = request.required_features - registered.descriptor.features
            if missing_features:
                names = ", ".join(sorted(feature.value for feature in missing_features))
                raise CapabilityUnavailable(
                    "capability.unsupported",
                    f"provider does not support required features: {names}",
                )
            binding_payload: JsonObject = {
                "provider_id": registered.provider_id,
                "provider_epoch": registered.registration.epoch,
            }
            binding_payload.update(ownership_payload(request.ownership))
            snapshot = await self.store.snapshot(request.invocation_id)
            if snapshot.status is InvocationStatus.REGISTERED:
                await self.store.append_event(
                    request.invocation_id,
                    InvocationStatus.PREFLIGHTING,
                    binding_payload,
                )
            elif not resume or snapshot.status is not InvocationStatus.PREFLIGHTING:
                raise ProviderContractError(
                    "recovery.status_not_resumable",
                    f"invocation status {snapshot.status.value} cannot be resumed here",
                    reconciliation_required=True,
                )
            for guard in tuple(self._guards):
                await guard.check(request)
            cancel_reason = self._cancel_requests.get(request.invocation_id)
            if cancel_reason is not None:
                await self.store.finalize(
                    InvocationResult(
                        invocation_id=request.invocation_id,
                        status=InvocationStatus.CANCELLED,
                        error_code="invocation.cancelled_before_start",
                        error_message=cancel_reason,
                    )
                )
                return
            self._starting.add(request.invocation_id)
            try:
                lifecycle = registered.prepared_lifecycle
                if lifecycle is None:
                    await self.store.append_event(
                        request.invocation_id,
                        InvocationStatus.STARTING,
                        {**binding_payload, "external_start_attempted": True},
                    )
                    start_attempted = True
                    try:
                        async with asyncio.timeout(self.provider_start_timeout_seconds):
                            provider_handle = await registered.provider.start(request)
                    except TimeoutError as exc:
                        raise ProviderExecutionError(
                            "provider.start_timeout",
                            "provider did not finish starting before the deadline",
                            reconciliation_required=True,
                        ) from exc
                else:
                    await self.store.append_event(
                        request.invocation_id,
                        InvocationStatus.RESOURCE_ACQUIRING,
                        binding_payload,
                    )
                    start_attempted = True
                    try:
                        async with asyncio.timeout(self.provider_start_timeout_seconds):
                            prepared_session = await lifecycle.prepare_session(request)
                    except TimeoutError as exc:
                        raise ProviderExecutionError(
                            "provider.prepare_timeout",
                            "provider session preparation exceeded the deadline",
                            reconciliation_required=True,
                        ) from exc
                    provider_session_id = prepared_session.provider_session_id
                    if not provider_session_id.strip():
                        raise ProviderContractError(
                            "provider.session_id_missing",
                            "prepared provider session must expose a non-empty id",
                        )
                    await self.store.append_event(
                        request.invocation_id,
                        InvocationStatus.PREPARED,
                        {
                            **binding_payload,
                            "provider_session_id": provider_session_id,
                        },
                    )
                    cancel_reason = self._cancel_requests.get(request.invocation_id)
                    if cancel_reason is not None:
                        await self._close_prepared_session(prepared_session)
                        prepared_session = None
                        await self.store.finalize(
                            InvocationResult(
                                invocation_id=request.invocation_id,
                                status=InvocationStatus.CANCELLED,
                                error_code="invocation.cancelled_before_turn",
                                error_message=cancel_reason,
                            )
                        )
                        return
                    await self.store.append_event(
                        request.invocation_id,
                        InvocationStatus.STARTING,
                        {
                            **binding_payload,
                            "provider_session_id": provider_session_id,
                            "external_start_attempted": True,
                        },
                    )
                    try:
                        async with asyncio.timeout(self.provider_start_timeout_seconds):
                            provider_handle = await lifecycle.start_turn(prepared_session)
                    except TimeoutError as exc:
                        raise ProviderExecutionError(
                            "provider.start_turn_timeout",
                            "provider turn start exceeded the deadline",
                            reconciliation_required=True,
                        ) from exc
                    prepared_handed_off = True
            finally:
                self._starting.discard(request.invocation_id)
                if prepared_session is not None and not prepared_handed_off:
                    await self._close_prepared_session(prepared_session)
                    prepared_session = None
            self._active_handles[request.invocation_id] = provider_handle
            cancel_reason = self._cancel_requests.get(request.invocation_id)
            if cancel_reason is None:
                running_payload: JsonObject = {
                    "provider_id": registered.provider_id,
                    "provider_epoch": registered.registration.epoch,
                    "external_start_attempted": True,
                }
                running_payload.update(_provider_handle_identity(provider_handle))
                await self.store.append_event(
                    request.invocation_id,
                    InvocationStatus.RUNNING,
                    running_payload,
                )
            else:
                await self._append_stopping(request.invocation_id, cancel_reason)
                await provider_handle.cancel(cancel_reason)
            stream_task = asyncio.create_task(self._consume_events(request, provider_handle))
            try:
                result = await provider_handle.wait()
                await stream_task
            finally:
                self._active_handles.pop(request.invocation_id, None)
                close_error = await self._close_provider_handle(provider_handle)
                if close_error is not None and result is None:
                    raise ProviderExecutionError(
                        "provider.close_failed",
                        close_error,
                        reconciliation_required=True,
                    )
            if result is None:
                raise ProviderContractError(
                    "provider.result_missing",
                    "provider did not return an invocation result",
                )
            if result.invocation_id != request.invocation_id:
                raise ProviderContractError(
                    "provider.result_id_mismatch",
                    "provider returned a result for another invocation",
                )
            output_schema = (
                request.output_schema
                if request.output_schema is not None
                else operation.output_schema
            )
            if (
                result.status is InvocationStatus.SUCCEEDED
                and output_schema
                and not matches_json_schema(result.output, output_schema)
            ):
                raise ProviderContractError(
                    "provider.output_schema_invalid",
                    "provider output does not satisfy capability operation "
                    f"{request.operation} schema",
                    reconciliation_required=False,
                )
            await self.store.append_event(
                request.invocation_id,
                InvocationStatus.FINALIZING,
                {},
            )
            await self.store.finalize(result)
        except CapabilityUnavailable as exc:
            await self._finalize_error(
                request,
                exc,
                reconciliation_required=False,
                rejected=True,
            )
        except InvocationRejected as exc:
            await self._finalize_error(
                request,
                exc,
                reconciliation_required=False,
                rejected=True,
            )
        except ProviderExecutionError as exc:
            await self._finalize_error(
                request,
                exc,
                reconciliation_required=exc.reconciliation_required,
            )
        except ProviderContractError as exc:
            await self._finalize_error(
                request,
                exc,
                reconciliation_required=exc.reconciliation_required,
            )
        except asyncio.CancelledError:
            if provider_handle is not None:
                try:
                    async with asyncio.timeout(self.cancellation_timeout_seconds):
                        await provider_handle.close()
                except Exception:
                    pass
            if not start_attempted:
                await self.store.finalize(
                    InvocationResult(
                        invocation_id=request.invocation_id,
                        status=InvocationStatus.CANCELLED,
                        error_code="invocation.cancelled_before_start",
                        error_message="invocation execution was cancelled before provider start",
                    )
                )
            else:
                await self._finalize_error(
                    request,
                    InvocationError(
                        "invocation.execution_aborted",
                        "invocation execution was aborted before provider termination was proven",
                    ),
                    reconciliation_required=True,
                )
        except Exception as exc:
            await self._finalize_error(
                request,
                exc,
                reconciliation_required=start_attempted,
            )
        finally:
            self._starting.discard(request.invocation_id)
            self._cancel_requests.pop(request.invocation_id, None)

    async def _consume_events(
        self,
        request: InvocationRequest,
        provider_handle: ProviderHandle,
    ) -> None:
        last_sequence = 0
        async for event in provider_handle.events():
            if event.invocation_id != request.invocation_id:
                raise ProviderContractError(
                    "provider.event_id_mismatch",
                    "provider emitted an event for another invocation",
                )
            if event.sequence != last_sequence + 1:
                raise ProviderContractError(
                    "provider.event_sequence_invalid",
                    "provider event sequence must start at one and be contiguous",
                )
            last_sequence = event.sequence
            try:
                await self.store.append_event(
                    request.invocation_id,
                    event.status,
                    event.payload,
                )
            except InvocationError as exc:
                if (
                    exc.code != "invocation.transition_invalid"
                    or event.status is not InvocationStatus.RUNNING
                ):
                    raise
                snapshot = await self.store.snapshot(request.invocation_id)
                if snapshot.status is not InvocationStatus.STOPPING:
                    raise
                # A provider may have queued progress before cancellation won the
                # race. Persist the observation without moving the invocation
                # backwards from stopping to running.
                await self.store.append_event(
                    request.invocation_id,
                    InvocationStatus.STOPPING,
                    event.payload,
                )

    async def _finalize_error(
        self,
        request: InvocationRequest,
        error: Exception,
        *,
        reconciliation_required: bool,
        rejected: bool = False,
    ) -> None:
        status = (
            InvocationStatus.RECONCILIATION_REQUIRED
            if reconciliation_required
            else InvocationStatus.REJECTED
            if rejected
            else InvocationStatus.FAILED
        )
        code = getattr(error, "code", type(error).__name__)
        snapshot = await self.store.snapshot(request.invocation_id)
        if snapshot.result is not None:
            return
        try:
            await self.store.finalize(
                InvocationResult(
                    invocation_id=request.invocation_id,
                    status=status,
                    error_code=code,
                    error_message=str(error),
                )
            )
        except InvocationError:
            snapshot = await self.store.snapshot(request.invocation_id)
            if snapshot.result is None:
                raise

    async def _append_stopping(self, invocation_id: str, reason: str) -> None:
        snapshot = await self.store.snapshot(invocation_id)
        if snapshot.result is None and snapshot.status is not InvocationStatus.STOPPING:
            try:
                await self.store.append_event(
                    invocation_id,
                    InvocationStatus.STOPPING,
                    {"reason": reason},
                )
            except InvocationError:
                snapshot = await self.store.snapshot(invocation_id)
                if snapshot.result is None:
                    raise

    async def _force_close(
        self,
        invocation_id: str,
        provider_handle: ProviderHandle,
        reason: str,
        *,
        error_code: str,
    ) -> None:
        try:
            async with asyncio.timeout(self.cancellation_timeout_seconds):
                await provider_handle.close()
        except Exception as exc:
            reason = f"{reason}; provider close failed: {exc}"
        await self._finalize_unproven_termination(
            invocation_id,
            error_code=error_code,
            reason=reason,
        )
        task = self._tasks.get(invocation_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _close_provider_handle(self, provider_handle: ProviderHandle) -> str | None:
        try:
            async with asyncio.timeout(self.cancellation_timeout_seconds):
                await provider_handle.close()
        except TimeoutError:
            return "provider handle close timed out"
        except Exception as exc:
            return f"provider handle close failed: {exc}"
        return None

    async def _close_prepared_session(
        self,
        prepared_session: PreparedProviderSession,
    ) -> None:
        cleanup_error = await prepared_session.close()
        if cleanup_error is not None:
            raise ProviderExecutionError(
                "provider.prepared_cleanup_failed",
                cleanup_error,
                reconciliation_required=True,
            )

    async def _reconcile_persisted(
        self,
        snapshot: InvocationSnapshot,
        registered: _RegisteredProvider,
    ) -> None:
        provider_execution = snapshot.provider_execution
        recovery = registered.persisted_recovery
        if provider_execution is None or recovery is None:
            await self._finalize_recovery_error(
                snapshot,
                InvocationError(
                    "recovery.provider_reconcile_unavailable",
                    "provider cannot reconcile persisted external execution",
                ),
            )
            return
        try:
            async with asyncio.timeout(self.provider_start_timeout_seconds):
                reconciled = await recovery.reconcile_persisted(
                    snapshot.request,
                    provider_execution,
                )
        except Exception as exc:
            await self._finalize_recovery_error(
                snapshot,
                InvocationError(
                    "recovery.provider_reconcile_failed",
                    str(exc),
                ),
            )
            return
        if reconciled.status not in {
            ReconcileStatus.SUCCEEDED,
            ReconcileStatus.FAILED,
            ReconcileStatus.CANCELLED,
        }:
            message = (
                reconciled.error_message
                or reconciled.message
                or (f"provider reconciliation returned {reconciled.status.value}")
            )
            await self._finalize_recovery_error(
                snapshot,
                InvocationError("recovery.external_state_unknown", message),
            )
            return
        if snapshot.status not in {InvocationStatus.RUNNING, InvocationStatus.STOPPING}:
            await self._finalize_recovery_error(
                snapshot,
                InvocationError(
                    "recovery.terminal_state_before_running",
                    "provider reported a terminal state for a non-running invocation",
                ),
            )
            return
        finalizing_payload: JsonObject = {
            "provider_id": provider_execution.provider_id,
            "provider_epoch": provider_execution.provider_epoch,
            "external_start_attempted": provider_execution.external_start_attempted,
        }
        if provider_execution.provider_session_id is not None:
            finalizing_payload["provider_session_id"] = provider_execution.provider_session_id
        if provider_execution.provider_operation_id is not None:
            finalizing_payload["provider_operation_id"] = provider_execution.provider_operation_id
        await self.store.append_event(
            snapshot.request.invocation_id,
            InvocationStatus.FINALIZING,
            finalizing_payload,
        )
        status = {
            ReconcileStatus.SUCCEEDED: InvocationStatus.SUCCEEDED,
            ReconcileStatus.FAILED: InvocationStatus.FAILED,
            ReconcileStatus.CANCELLED: InvocationStatus.CANCELLED,
        }[reconciled.status]
        await self.store.finalize(
            InvocationResult(
                invocation_id=snapshot.request.invocation_id,
                status=status,
                output=reconciled.output,
                error_code=reconciled.error_code,
                error_message=reconciled.error_message,
            )
        )

    async def _finalize_recovery_error(
        self,
        snapshot: InvocationSnapshot,
        error: Exception,
    ) -> None:
        await self._finalize_error(
            snapshot.request,
            error,
            reconciliation_required=True,
        )

    async def _finalize_unproven_termination(
        self,
        invocation_id: str,
        *,
        error_code: str,
        reason: str,
    ) -> None:
        snapshot = await self.store.snapshot(invocation_id)
        if snapshot.result is not None:
            return
        try:
            await self.store.finalize(
                InvocationResult(
                    invocation_id=invocation_id,
                    status=InvocationStatus.RECONCILIATION_REQUIRED,
                    error_code=error_code,
                    error_message=reason,
                )
            )
        except InvocationError:
            snapshot = await self.store.snapshot(invocation_id)
            if snapshot.result is None:
                raise


def _reconcile_from_terminal(result: InvocationResult) -> ReconcileResult:
    mapping = {
        InvocationStatus.SUCCEEDED: ReconcileStatus.SUCCEEDED,
        InvocationStatus.REJECTED: ReconcileStatus.FAILED,
        InvocationStatus.FAILED: ReconcileStatus.FAILED,
        InvocationStatus.CANCELLED: ReconcileStatus.CANCELLED,
        InvocationStatus.RECONCILIATION_REQUIRED: ReconcileStatus.UNREACHABLE,
    }
    return ReconcileResult(
        mapping[result.status],
        output=result.output,
        error_code=result.error_code,
        error_message=result.error_message,
    )


def _prepared_lifecycle(provider: InvocationProvider) -> _PreparedLifecycle | None:
    prepare_session = getattr(provider, "prepare_session", None)
    start_turn = getattr(provider, "start_turn", None)
    if prepare_session is None and start_turn is None:
        return None
    if not callable(prepare_session) or not callable(start_turn):
        raise ProviderContractError(
            "provider.prepared_lifecycle_incomplete",
            "provider must implement both prepare_session and start_turn",
            reconciliation_required=False,
        )
    return _PreparedLifecycle(
        cast(
            Callable[[InvocationRequest], Awaitable[PreparedProviderSession]],
            prepare_session,
        ),
        cast(
            Callable[[PreparedProviderSession], Awaitable[ProviderHandle]],
            start_turn,
        ),
    )


def _persisted_recovery(provider: InvocationProvider) -> PersistedProviderRecovery | None:
    reconcile_persisted = getattr(provider, "reconcile_persisted", None)
    if reconcile_persisted is None:
        return None
    if not callable(reconcile_persisted):
        raise ProviderContractError(
            "provider.persisted_recovery_invalid",
            "provider reconcile_persisted must be callable",
            reconciliation_required=False,
        )
    return cast(PersistedProviderRecovery, provider)


def _provider_handle_identity(provider_handle: ProviderHandle) -> JsonObject:
    payload: JsonObject = {}
    for attribute, field_name in (
        ("provider_session_id", "provider_session_id"),
        ("provider_operation_id", "provider_operation_id"),
    ):
        value = getattr(provider_handle, attribute, None)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ProviderContractError(
                "provider.identity_invalid",
                f"{attribute} must be a non-empty string when exposed",
            )
        payload[field_name] = value.strip()
    return payload
