from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import cast

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

from misaka_invocation_runtime.errors import (
    CapabilityUnavailable,
    InvocationError,
    InvocationRejected,
    ProviderContractError,
    ProviderExecutionError,
)
from misaka_invocation_runtime.provider import InvocationGuard, InvocationProvider, ProviderHandle
from misaka_invocation_runtime.store import (
    InvocationSnapshot,
    InvocationStore,
    MemoryInvocationStore,
)


@dataclass(frozen=True, slots=True)
class _RegisteredProvider:
    provider_id: str
    provider: InvocationProvider
    descriptor: CapabilityDescriptor


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

    async def register_provider(self, provider_id: str, provider: InvocationProvider) -> None:
        if self._stopping:
            raise InvocationError("runtime.stopping", "runtime is stopping")
        if not provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if provider_id in self._providers:
            raise InvocationError(
                "provider.duplicate", f"provider {provider_id} is already registered"
            )
        descriptor = await provider.describe()
        self._providers[provider_id] = _RegisteredProvider(provider_id, provider, descriptor)

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(item.descriptor for item in self._providers.values())

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
        task = asyncio.create_task(self._execute(request, provider))
        self._tasks[request.invocation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(request.invocation_id, None))
        return handle

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
    ) -> None:
        provider_handle: ProviderHandle | None = None
        start_attempted = False
        result: InvocationResult | None = None
        try:
            operation_names = {operation.name for operation in registered.descriptor.operations}
            if request.operation not in operation_names:
                raise CapabilityUnavailable(
                    "capability.operation_unavailable",
                    f"operation {request.operation} is not supported",
                )
            missing_features = request.required_features - registered.descriptor.features
            if missing_features:
                names = ", ".join(sorted(feature.value for feature in missing_features))
                raise CapabilityUnavailable(
                    "capability.unsupported",
                    f"provider does not support required features: {names}",
                )
            await self.store.append_event(
                request.invocation_id,
                InvocationStatus.PREFLIGHTING,
                {},
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
            await self.store.append_event(request.invocation_id, InvocationStatus.STARTING, {})
            start_attempted = True
            self._starting.add(request.invocation_id)
            try:
                try:
                    async with asyncio.timeout(self.provider_start_timeout_seconds):
                        provider_handle = await registered.provider.start(request)
                except TimeoutError as exc:
                    raise ProviderExecutionError(
                        "provider.start_timeout",
                        "provider did not finish starting before the deadline",
                        reconciliation_required=True,
                    ) from exc
            finally:
                self._starting.discard(request.invocation_id)
            self._active_handles[request.invocation_id] = provider_handle
            cancel_reason = self._cancel_requests.get(request.invocation_id)
            if cancel_reason is None:
                await self.store.append_event(request.invocation_id, InvocationStatus.RUNNING, {})
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
            await self._finalize_error(request, exc, reconciliation_required=True)
        except asyncio.CancelledError:
            if provider_handle is not None:
                try:
                    async with asyncio.timeout(self.cancellation_timeout_seconds):
                        await provider_handle.close()
                except Exception:
                    pass
            await self._finalize_error(
                request,
                InvocationError(
                    "invocation.execution_aborted",
                    "invocation execution was aborted before provider termination was proven",
                ),
                reconciliation_required=start_attempted,
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
            await self.store.append_event(
                request.invocation_id,
                event.status,
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
