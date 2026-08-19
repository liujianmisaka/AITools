from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from misaka_kernel_contracts import JsonObject

from misaka_coordinator_runtime.contracts import (
    CoordinatorEvent,
    CoordinatorStatus,
    ExecutionPlan,
    ExecutionResult,
    ExecutionStatus,
    QueueJobResult,
    QueueJobSnapshot,
    QueueJobStatus,
)
from misaka_coordinator_runtime.direct import DirectExecutionHandle
from misaka_coordinator_runtime.errors import (
    CoordinatorConflict,
    CoordinatorNotFound,
    CoordinatorStateError,
    QueueCapacityExceeded,
)
from misaka_coordinator_runtime.start import start_execution


@dataclass(slots=True)
class _QueueJob:
    job_id: str
    plan: ExecutionPlan
    max_attempts: int
    fingerprint: str
    status: QueueJobStatus = QueueJobStatus.QUEUED
    attempts: list[ExecutionResult] = field(default_factory=list)
    events: list[CoordinatorEvent] = field(default_factory=list)
    result: QueueJobResult | None = None
    handle: DirectExecutionHandle | None = None
    cancel_requested: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


class QueueJobHandle:
    def __init__(self, coordinator: QueueCoordinator, job_id: str) -> None:
        self._coordinator = coordinator
        self.job_id = job_id

    async def wait(self) -> QueueJobResult:
        return await self._coordinator.wait(self.job_id)

    async def cancel(self, reason: str) -> None:
        await self._coordinator.cancel(self.job_id, reason)

    async def snapshot(self) -> QueueJobSnapshot:
        return await self._coordinator.snapshot(self.job_id)

    def events(self, *, start_sequence: int = 1) -> AsyncIterator[CoordinatorEvent]:
        return self._coordinator.events(self.job_id, start_sequence=start_sequence)


class QueueCoordinator:
    """Bounded queue over provider-neutral ExecutionPlans."""

    def __init__(
        self,
        *,
        capacity: int = 100,
        worker_count: int = 1,
        default_max_attempts: int = 1,
        shutdown_timeout_seconds: float = 15.0,
    ) -> None:
        if capacity < 1 or worker_count < 1 or default_max_attempts < 1:
            raise ValueError("capacity, worker_count and default_max_attempts must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._queue: asyncio.Queue[_QueueJob] = asyncio.Queue(maxsize=capacity)
        self._worker_count = worker_count
        self._default_max_attempts = default_max_attempts
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._status = CoordinatorStatus.STOPPED
        self._jobs: dict[str, _QueueJob] = {}
        self._workers: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._stopped.set()

    @property
    def status(self) -> CoordinatorStatus:
        return self._status

    @property
    def queued_count(self) -> int:
        return self._queue.qsize()

    @property
    def active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job.status is QueueJobStatus.RUNNING)

    async def start(self) -> None:
        async with self._lock:
            if self._status is CoordinatorStatus.ACTIVE:
                return
            if self._status is CoordinatorStatus.STOPPING:
                raise CoordinatorStateError(
                    "coordinator.stopping",
                    "queue coordinator is stopping",
                )
            self._status = CoordinatorStatus.ACTIVE
            self._stopped.clear()
            for _ in range(self._worker_count):
                worker = asyncio.create_task(self._worker())
                self._workers.add(worker)
                worker.add_done_callback(self._workers.discard)

    async def submit(
        self,
        job_id: str,
        plan: ExecutionPlan,
        *,
        max_attempts: int | None = None,
    ) -> QueueJobHandle:
        if not job_id.strip():
            raise ValueError("job_id must not be empty")
        attempts_limit = self._default_max_attempts if max_attempts is None else max_attempts
        if attempts_limit < 1:
            raise ValueError("max_attempts must be at least one")
        async with self._lock:
            if self._status is not CoordinatorStatus.ACTIVE:
                raise CoordinatorStateError(
                    "coordinator.not_active",
                    "queue coordinator is not active",
                )
            existing = self._jobs.get(job_id)
            fingerprint = f"{plan.fingerprint}:attempts:{attempts_limit}"
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise CoordinatorConflict(
                        "queue.job_conflict",
                        f"job {job_id} already exists with different request",
                    )
                return QueueJobHandle(self, job_id)
            if self._queue.full():
                raise QueueCapacityExceeded(
                    "queue.capacity_exceeded",
                    "queue capacity has been reached",
                )
            job = _QueueJob(
                job_id=job_id,
                plan=plan,
                max_attempts=attempts_limit,
                fingerprint=fingerprint,
            )
            self._jobs[job_id] = job
            await self._append_event(job, QueueJobStatus.QUEUED, {"max_attempts": attempts_limit})
            self._queue.put_nowait(job)
            return QueueJobHandle(self, job_id)

    async def snapshot(self, job_id: str) -> QueueJobSnapshot:
        job = self._job(job_id)
        async with job.condition:
            return QueueJobSnapshot(
                job_id=job.job_id,
                status=job.status,
                attempts=tuple(job.attempts),
                result=job.result.result if job.result is not None else None,
            )

    async def wait(self, job_id: str) -> QueueJobResult:
        job = self._job(job_id)
        async with job.condition:
            while job.result is None:
                await job.condition.wait()
            return job.result

    async def events(
        self,
        job_id: str,
        *,
        start_sequence: int = 1,
    ) -> AsyncIterator[CoordinatorEvent]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        job = self._job(job_id)
        index = start_sequence - 1
        while True:
            async with job.condition:
                while index >= len(job.events) and job.result is None:
                    await job.condition.wait()
                if index >= len(job.events):
                    return
                event = job.events[index]
                index += 1
            yield event

    async def cancel(self, job_id: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        job = self._job(job_id)
        async with job.condition:
            if job.result is not None:
                return
            job.cancel_requested = True
            handle = job.handle
            queued = job.status in {QueueJobStatus.QUEUED, QueueJobStatus.RETRYING}
        if handle is not None:
            await handle.cancel(reason)
        elif queued:
            await self._finish_cancelled(job, reason)
        else:
            await self._finish_reconciliation(
                job,
                "queue.cancel_handle_missing",
                "the execution handle was not available to prove cancellation",
            )

    async def stop(self) -> None:
        async with self._lock:
            if self._status is CoordinatorStatus.STOPPED and not self._workers:
                return
            if self._status is CoordinatorStatus.STOPPING:
                wait_for_existing_stop = True
            else:
                self._status = CoordinatorStatus.STOPPING
                wait_for_existing_stop = False
        if wait_for_existing_stop:
            await self._stopped.wait()
            return
        jobs = tuple(self._jobs.values())
        await asyncio.gather(
            *(self.cancel(job.job_id, "queue coordinator stopping") for job in jobs),
            return_exceptions=True,
        )
        workers = tuple(self._workers)
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                await self._queue.join()
                for worker in workers:
                    worker.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
        except TimeoutError:
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            for job in jobs:
                if job.result is None:
                    await self._finish_reconciliation(
                        job,
                        "queue.shutdown_timeout",
                        "queue worker did not stop before the shutdown deadline",
                    )
        finally:
            self._workers.clear()
            self._status = CoordinatorStatus.STOPPED
            self._stopped.set()

    async def _worker(self) -> None:
        while True:
            if self._status is CoordinatorStatus.STOPPING and self._queue.empty():
                return
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._run_job(job)
            finally:
                self._queue.task_done()

    async def _run_job(self, job: _QueueJob) -> None:
        for attempt in range(1, job.max_attempts + 1):
            async with job.condition:
                if job.result is not None:
                    return
                should_cancel = job.cancel_requested
            if should_cancel:
                await self._finish_cancelled(job, "queue job cancelled before execution")
                return
            await self._append_event(job, QueueJobStatus.RUNNING, {"attempt": attempt})
            try:
                handle = DirectExecutionHandle(
                    await start_execution(
                        job.plan,
                        attempt=attempt,
                        cancellation_reason="queue job execution cancelled during start",
                    )
                )
                async with job.condition:
                    job.handle = handle
                    cancel_requested = job.cancel_requested
                if cancel_requested:
                    await handle.cancel("queue job cancellation requested during start")
                result = await handle.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = ExecutionResult(
                    execution_id=f"{job.job_id}:attempt:{attempt}",
                    status=ExecutionStatus.FAILED,
                    error_code=getattr(exc, "code", type(exc).__name__),
                    error_message=str(exc),
                )
            finally:
                async with job.condition:
                    job.handle = None
            job.attempts.append(result)
            if result.status is ExecutionStatus.SUCCEEDED:
                await self._finish(job, QueueJobStatus.SUCCEEDED, result)
                return
            if result.status is ExecutionStatus.CANCELLED:
                await self._finish(job, QueueJobStatus.CANCELLED, result)
                return
            if result.status is ExecutionStatus.RECONCILIATION_REQUIRED:
                await self._finish(job, QueueJobStatus.RECONCILIATION_REQUIRED, result)
                return
            if attempt < job.max_attempts and result.status is ExecutionStatus.FAILED:
                await self._append_event(
                    job,
                    QueueJobStatus.RETRYING,
                    {"attempt": attempt, "error_code": result.error_code},
                )
                continue
            await self._finish(job, QueueJobStatus.FAILED, result)
            return

    async def _finish(
        self,
        job: _QueueJob,
        status: QueueJobStatus,
        result: ExecutionResult,
    ) -> None:
        async with job.condition:
            if job.result is not None:
                return
            job.status = status
            job.result = QueueJobResult(
                job_id=job.job_id,
                status=status,
                result=result,
                attempts=tuple(job.attempts),
                error_message=result.error_message,
            )
            job.events.append(
                CoordinatorEvent(
                    job_id=job.job_id,
                    sequence=len(job.events) + 1,
                    status=status,
                    payload=_result_payload(result),
                )
            )
            job.condition.notify_all()

    async def _finish_cancelled(self, job: _QueueJob, reason: str) -> None:
        result = ExecutionResult(
            execution_id=f"{job.job_id}:cancelled",
            status=ExecutionStatus.CANCELLED,
            error_code="queue.cancelled",
            error_message=reason,
        )
        await self._finish(job, QueueJobStatus.CANCELLED, result)

    async def _finish_reconciliation(
        self,
        job: _QueueJob,
        code: str,
        message: str,
    ) -> None:
        result = ExecutionResult(
            execution_id=f"{job.job_id}:reconciliation",
            status=ExecutionStatus.RECONCILIATION_REQUIRED,
            error_code=code,
            error_message=message,
        )
        await self._finish(job, QueueJobStatus.RECONCILIATION_REQUIRED, result)

    async def _append_event(
        self,
        job: _QueueJob,
        status: QueueJobStatus,
        payload: JsonObject,
    ) -> None:
        async with job.condition:
            if job.result is not None:
                return
            job.status = status
            job.events.append(
                CoordinatorEvent(
                    job_id=job.job_id,
                    sequence=len(job.events) + 1,
                    status=status,
                    payload=payload,
                )
            )
            job.condition.notify_all()

    def _job(self, job_id: str) -> _QueueJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise CoordinatorNotFound(
                "queue.job_not_found",
                f"queue job {job_id} was not found",
            ) from exc


def _result_payload(result: ExecutionResult) -> JsonObject:
    payload: JsonObject = {"execution_id": result.execution_id}
    if result.activation_id is not None:
        payload["activation_id"] = result.activation_id
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    if result.error_message is not None:
        payload["error_message"] = result.error_message
    return payload
