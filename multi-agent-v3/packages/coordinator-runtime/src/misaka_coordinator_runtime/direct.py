from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from misaka_coordinator_runtime.contracts import (
    CoordinatorStatus,
    ExecutionEvent,
    ExecutionHandle,
    ExecutionPlan,
    ExecutionResult,
    ReconciliationResult,
)
from misaka_coordinator_runtime.errors import CoordinatorStateError


class DirectExecutionHandle:
    def __init__(self, handle: ExecutionHandle) -> None:
        self._handle = handle

    @property
    def execution_id(self) -> str:
        return self._handle.execution_id

    @property
    def activation_id(self) -> str | None:
        return self._handle.activation_id

    def events(self, *, start_sequence: int = 1) -> AsyncIterator[ExecutionEvent]:
        return self._handle.events(start_sequence=start_sequence)

    async def wait(self) -> ExecutionResult:
        return await self._handle.wait()

    async def cancel(self, reason: str) -> None:
        await self._handle.cancel(reason)

    async def reconcile(self) -> ReconciliationResult:
        return await self._handle.reconcile()


class DirectCoordinator:
    """Coordinates one ExecutionPlan without owning execution semantics."""

    def __init__(self, *, shutdown_timeout_seconds: float = 15.0) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._status = CoordinatorStatus.STOPPED
        self._active: dict[str, DirectExecutionHandle] = {}
        self._handles: dict[str, DirectExecutionHandle] = {}
        self._starting: set[str] = set()
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

    async def submit(self, plan: ExecutionPlan) -> DirectExecutionHandle:
        async with self._lock:
            if self._status is not CoordinatorStatus.ACTIVE:
                raise CoordinatorStateError(
                    "coordinator.not_active",
                    "direct coordinator must be active before submission",
                )
            if plan.execution_id in self._handles or plan.execution_id in self._starting:
                raise CoordinatorStateError(
                    "coordinator.execution_duplicate",
                    f"execution {plan.execution_id} is already managed",
                )
            self._starting.add(plan.execution_id)
        try:
            handle = DirectExecutionHandle(await plan.start(attempt=1))
        except BaseException:
            async with self._lock:
                self._starting.discard(plan.execution_id)
            raise
        cancel_reason: str | None = None
        submission_error: CoordinatorStateError | None = None
        async with self._lock:
            self._starting.discard(plan.execution_id)
            if self._status is not CoordinatorStatus.ACTIVE:
                cancel_reason = "direct coordinator stopped during submission"
                submission_error = CoordinatorStateError(
                    "coordinator.stopping",
                    "direct coordinator stopped while submission was starting",
                )
            elif handle.execution_id in self._handles:
                cancel_reason = "duplicate execution identity"
                submission_error = CoordinatorStateError(
                    "coordinator.execution_duplicate",
                    f"execution {handle.execution_id} is already managed",
                )
            else:
                self._active[handle.execution_id] = handle
                self._handles[handle.execution_id] = handle
                observer = asyncio.create_task(self._observe(handle))
                self._observers.add(observer)
                observer.add_done_callback(self._observers.discard)
        if submission_error is not None:
            await handle.cancel(cancel_reason or "direct submission rejected")
            raise submission_error
        return handle

    async def wait(self, execution_id: str) -> ExecutionResult:
        handle = self._handles.get(execution_id)
        if handle is None:
            raise KeyError(execution_id)
        return await handle.wait()

    async def cancel(self, execution_id: str, reason: str) -> None:
        handle = self._handles.get(execution_id)
        if handle is None:
            raise KeyError(execution_id)
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
            self._active.pop(handle.execution_id, None)
