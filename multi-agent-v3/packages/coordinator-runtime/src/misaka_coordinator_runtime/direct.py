from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from misaka_invocation_contracts import (
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    ReconcileResult,
)
from misaka_invocation_runtime import InvocationRuntime, RuntimeInvocationHandle

from misaka_coordinator_runtime.contracts import CoordinatorStatus
from misaka_coordinator_runtime.errors import CoordinatorStateError


class DirectExecutionHandle:
    def __init__(self, handle: RuntimeInvocationHandle) -> None:
        self._handle = handle

    @property
    def invocation_id(self) -> str:
        return self._handle.invocation_id

    async def events(self, *, start_sequence: int = 1) -> AsyncIterator[InvocationEvent]:
        async for event in self._handle.events(start_sequence=start_sequence):
            yield event

    async def wait(self) -> InvocationResult:
        return await self._handle.wait()

    async def cancel(self, reason: str) -> None:
        await self._handle.cancel(reason)

    async def reconcile(self) -> ReconcileResult:
        return await self._handle.reconcile()

    async def snapshot(self):
        return await self._handle.snapshot()


class DirectCoordinator:
    """Small coordinator that owns submission and cancellation, not execution semantics."""

    def __init__(
        self,
        runtime: InvocationRuntime,
        *,
        shutdown_timeout_seconds: float = 15.0,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._runtime = runtime
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._status = CoordinatorStatus.STOPPED
        self._active: dict[str, DirectExecutionHandle] = {}
        self._handles: dict[str, DirectExecutionHandle] = {}
        self._observers: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._stopped.set()

    @property
    def status(self) -> CoordinatorStatus:
        return self._status

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def start(self) -> None:
        async with self._lock:
            if self._status is CoordinatorStatus.ACTIVE:
                return
            if self._status is CoordinatorStatus.STOPPING:
                raise CoordinatorStateError(
                    "coordinator.stopping",
                    "direct coordinator is stopping",
                )
            self._status = CoordinatorStatus.ACTIVE
            self._stopped.clear()

    async def submit(
        self,
        request: InvocationRequest,
        *,
        provider_id: str | None = None,
    ) -> DirectExecutionHandle:
        async with self._lock:
            if self._status is not CoordinatorStatus.ACTIVE:
                raise CoordinatorStateError(
                    "coordinator.not_active",
                    "direct coordinator must be active before submission",
                )
            runtime_handle = await self._runtime.submit(request, provider_id=provider_id)
            handle = DirectExecutionHandle(runtime_handle)
            self._active[handle.invocation_id] = handle
            self._handles[handle.invocation_id] = handle
            observer = asyncio.create_task(self._observe(handle))
            self._observers.add(observer)
            observer.add_done_callback(self._observers.discard)
            return handle

    async def wait(self, invocation_id: str) -> InvocationResult:
        handle = self._handles.get(invocation_id)
        if handle is None:
            raise KeyError(invocation_id)
        return await handle.wait()

    async def cancel(self, invocation_id: str, reason: str) -> None:
        handle = self._handles.get(invocation_id)
        if handle is None:
            raise KeyError(invocation_id)
        await handle.cancel(reason)

    async def stop(self) -> None:
        async with self._lock:
            if self._status is CoordinatorStatus.STOPPED:
                return
            if self._status is CoordinatorStatus.STOPPING:
                wait_for_existing_stop = True
            else:
                self._status = CoordinatorStatus.STOPPING
                wait_for_existing_stop = False
        if wait_for_existing_stop:
            await self._stopped.wait()
            return
        handles = tuple(self._active.values())
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                await asyncio.gather(
                    *(handle.cancel("direct coordinator stopping") for handle in handles),
                    return_exceptions=True,
                )
                if self._observers:
                    await asyncio.gather(*tuple(self._observers), return_exceptions=True)
        except TimeoutError:
            for observer in tuple(self._observers):
                observer.cancel()
            if self._observers:
                await asyncio.gather(*tuple(self._observers), return_exceptions=True)
        finally:
            self._active.clear()
            self._handles.clear()
            self._status = CoordinatorStatus.STOPPED
            self._stopped.set()

    async def _observe(self, handle: DirectExecutionHandle) -> None:
        try:
            await handle.wait()
        finally:
            self._active.pop(handle.invocation_id, None)
