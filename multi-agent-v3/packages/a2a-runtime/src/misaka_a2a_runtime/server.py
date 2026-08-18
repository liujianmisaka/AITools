from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum

from misaka_a2a_capability import (
    TERMINAL_TASK_STATUSES,
    A2AAgentCard,
    A2AServerStateError,
    MemoryTaskStore,
    TaskCapabilityRejected,
    TaskEvent,
    TaskExecutionHandle,
    TaskHandler,
    TaskRequest,
    TaskResult,
    TaskSnapshot,
    TaskStatus,
    TaskStore,
)


class A2AServerStatus(StrEnum):
    STOPPED = "stopped"
    ACTIVE = "active"
    STOPPING = "stopping"


@dataclass(slots=True)
class _ActiveTask:
    execution: TaskExecutionHandle
    bridge: asyncio.Task[None]


class A2AServer:
    """Transport-neutral A2A task facade with bounded ownership semantics."""

    def __init__(
        self,
        handler: TaskHandler,
        *,
        store: TaskStore | None = None,
        submission_timeout_seconds: float = 15.0,
        shutdown_timeout_seconds: float = 15.0,
    ) -> None:
        if submission_timeout_seconds <= 0 or shutdown_timeout_seconds <= 0:
            raise ValueError("A2A server timeout values must be positive")
        self._handler = handler
        self.store = store or MemoryTaskStore()
        self.submission_timeout_seconds = submission_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._status = A2AServerStatus.STOPPED
        self._card: A2AAgentCard | None = None
        self._active: dict[str, _ActiveTask] = {}
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._stopped.set()

    @property
    def status(self) -> A2AServerStatus:
        return self._status

    @property
    def active_task_count(self) -> int:
        return len(self._active)

    async def start(self) -> None:
        async with self._lock:
            if self._status is A2AServerStatus.ACTIVE:
                return
            if self._status is A2AServerStatus.STOPPING:
                raise A2AServerStateError(
                    "a2a.server_stopping",
                    "A2A server is stopping",
                )
            self._card = await self._handler.describe()
            self._status = A2AServerStatus.ACTIVE
            self._stopped.clear()

    async def describe(self) -> A2AAgentCard:
        self._require_active()
        if self._card is None:
            raise RuntimeError("active A2A server has no agent card")
        return self._card

    async def submit(self, request: TaskRequest) -> TaskExecutionHandle:
        self._require_active()
        card = await self.describe()
        _validate_request(card, request)
        snapshot, created = await self.store.create(request)
        canonical_task_id = snapshot.request.task_id
        if not created:
            return StoredTaskExecutionHandle(
                self, canonical_task_id, invocation_id=snapshot.invocation_id
            )

        async with self._lock:
            if self._status is not A2AServerStatus.ACTIVE:
                await self.store.finalize(
                    TaskResult(
                        task_id=canonical_task_id,
                        invocation_id=None,
                        status=TaskStatus.REJECTED,
                        error_code="a2a.server_stopping",
                        error_message="A2A server stopped while accepting the task",
                    )
                )
                return StoredTaskExecutionHandle(
                    self,
                    canonical_task_id,
                    invocation_id=None,
                )
            try:
                async with asyncio.timeout(self.submission_timeout_seconds):
                    execution = await self._handler.submit(snapshot.request)
            except TimeoutError:
                await self.store.finalize(
                    TaskResult(
                        task_id=canonical_task_id,
                        invocation_id=None,
                        status=TaskStatus.RECONCILIATION_REQUIRED,
                        error_code="a2a.handler_submit_timeout",
                        error_message="task handler did not finish submission before the deadline",
                    )
                )
                return StoredTaskExecutionHandle(self, canonical_task_id, invocation_id=None)
            except Exception as exc:
                await self.store.finalize(
                    TaskResult(
                        task_id=canonical_task_id,
                        invocation_id=None,
                        status=TaskStatus.REJECTED,
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=str(exc),
                    )
                )
                return StoredTaskExecutionHandle(self, canonical_task_id, invocation_id=None)

            if execution.invocation_id is None:
                await self.store.finalize(
                    TaskResult(
                        task_id=canonical_task_id,
                        invocation_id=None,
                        status=TaskStatus.RECONCILIATION_REQUIRED,
                        error_code="a2a.invocation_identity_missing",
                        error_message="task handler started without an invocation identity",
                    )
                )
                await execution.close()
                return StoredTaskExecutionHandle(self, canonical_task_id, invocation_id=None)

            await self.store.mark_working(canonical_task_id, execution.invocation_id)
            bridge = asyncio.create_task(self._drive(canonical_task_id, execution))
            self._active[canonical_task_id] = _ActiveTask(execution, bridge)
        return StoredTaskExecutionHandle(
            self,
            canonical_task_id,
            invocation_id=execution.invocation_id,
        )

    async def snapshot(self, task_id: str) -> TaskSnapshot:
        return await self.store.snapshot(task_id)

    async def wait(self, task_id: str) -> TaskResult:
        return await self.store.wait_terminal(task_id)

    def events(
        self,
        task_id: str,
        *,
        start_sequence: int = 1,
    ) -> AsyncIterator[TaskEvent]:
        return self.store.events(task_id, start_sequence=start_sequence)

    async def cancel(self, task_id: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        snapshot = await self.store.snapshot(task_id)
        if snapshot.result is not None:
            return
        active = self._active.get(task_id)
        if active is None:
            await self.store.finalize(
                TaskResult(
                    task_id=task_id,
                    invocation_id=snapshot.invocation_id,
                    status=TaskStatus.RECONCILIATION_REQUIRED,
                    error_code="a2a.cancel_handle_missing",
                    error_message="task execution handle is unavailable",
                )
            )
            return
        if snapshot.status is not TaskStatus.CANCELLING:
            await self.store.append_event(
                task_id,
                TaskStatus.CANCELLING,
                {"reason": reason},
            )
        await active.execution.cancel(reason)

    async def stop(self) -> None:
        wait_for_existing_stop = False
        async with self._lock:
            if self._status is A2AServerStatus.STOPPED:
                return
            if self._status is A2AServerStatus.STOPPING:
                wait_for_existing_stop = True
            else:
                self._status = A2AServerStatus.STOPPING

        if wait_for_existing_stop:
            await self._stopped.wait()
            return

        active_items = tuple(self._active.items())
        try:
            async with asyncio.timeout(self.shutdown_timeout_seconds):
                await asyncio.gather(
                    *(self.cancel(task_id, "A2A server stopping") for task_id, _ in active_items),
                    return_exceptions=True,
                )
                if active_items:
                    await asyncio.gather(
                        *(active.bridge for _, active in active_items),
                        return_exceptions=True,
                    )
        except TimeoutError:
            await asyncio.gather(
                *(active.execution.close() for _, active in active_items),
                return_exceptions=True,
            )
            for task_id, active in active_items:
                if not active.bridge.done():
                    active.bridge.cancel()
                snapshot = await self.store.snapshot(task_id)
                if snapshot.result is None:
                    await self.store.finalize(
                        TaskResult(
                            task_id=task_id,
                            invocation_id=snapshot.invocation_id,
                            status=TaskStatus.RECONCILIATION_REQUIRED,
                            error_code="a2a.shutdown_timeout",
                            error_message="task did not stop before the A2A shutdown deadline",
                        )
                    )
            if active_items:
                await asyncio.gather(
                    *(active.bridge for _, active in active_items),
                    return_exceptions=True,
                )
        finally:
            self._active.clear()
            self._status = A2AServerStatus.STOPPED
            self._stopped.set()

    async def _drive(
        self,
        task_id: str,
        execution: TaskExecutionHandle,
    ) -> None:
        try:
            async for event in execution.events():
                if event.task_id != task_id:
                    raise RuntimeError("task handler emitted an event for another task")
                if event.status in TERMINAL_TASK_STATUSES:
                    continue
                snapshot = await self.store.snapshot(task_id)
                if snapshot.result is not None:
                    break
                if event.status is TaskStatus.SUBMITTED:
                    continue
                if event.status is TaskStatus.WORKING and snapshot.status is TaskStatus.CANCELLING:
                    continue
                await self.store.append_event(task_id, event.status, event.payload)
            result = await execution.wait()
            await self.store.finalize(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            snapshot = await self.store.snapshot(task_id)
            if snapshot.result is None:
                await self.store.finalize(
                    TaskResult(
                        task_id=task_id,
                        invocation_id=snapshot.invocation_id,
                        status=TaskStatus.RECONCILIATION_REQUIRED,
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=str(exc),
                    )
                )
        finally:
            self._active.pop(task_id, None)

    def _require_active(self) -> None:
        if self._status is not A2AServerStatus.ACTIVE:
            raise A2AServerStateError(
                "a2a.server_not_active",
                "A2A server must be active before accepting requests",
            )


class StoredTaskExecutionHandle:
    def __init__(
        self,
        server: A2AServer,
        task_id: str,
        *,
        invocation_id: str | None,
    ) -> None:
        self._server = server
        self._task_id = task_id
        self._invocation_id = invocation_id

    @property
    def invocation_id(self) -> str | None:
        return self._invocation_id

    def events(
        self,
        *,
        start_sequence: int = 1,
    ) -> AsyncIterator[TaskEvent]:
        return self._server.events(self._task_id, start_sequence=start_sequence)

    async def wait(self) -> TaskResult:
        return await self._server.wait(self._task_id)

    async def cancel(self, reason: str) -> None:
        await self._server.cancel(self._task_id, reason)

    async def close(self) -> None:
        return None


def _validate_request(card: A2AAgentCard, request: TaskRequest) -> None:
    skill = next(
        (
            candidate
            for candidate in card.skills
            if candidate.capability_id == request.capability_id
            and candidate.operation == request.operation
        ),
        None,
    )
    if skill is None:
        raise TaskCapabilityRejected(
            "a2a.operation_unsupported",
            f"A2A agent does not expose {request.capability_id}/{request.operation}",
        )
    missing_features = request.required_features - skill.features
    if missing_features:
        names = ", ".join(sorted(feature.value for feature in missing_features))
        raise TaskCapabilityRejected(
            "a2a.feature_unsupported",
            f"A2A skill does not support required features: {names}",
        )
    input_size = len(
        json.dumps(
            request.input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if input_size > card.max_input_bytes:
        raise TaskCapabilityRejected(
            "a2a.input_too_large",
            f"A2A task input exceeds {card.max_input_bytes} bytes",
        )
