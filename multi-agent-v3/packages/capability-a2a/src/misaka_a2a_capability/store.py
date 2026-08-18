from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from misaka_kernel_contracts import JsonObject

from misaka_a2a_capability.contracts import (
    TERMINAL_TASK_STATUSES,
    TaskEvent,
    TaskRequest,
    TaskResult,
    TaskSnapshot,
    TaskStatus,
    task_request_fingerprint,
)
from misaka_a2a_capability.errors import (
    TaskIdempotencyConflict,
    TaskNotFound,
    TaskStateError,
)


@dataclass(slots=True)
class _StoredTask:
    request: TaskRequest
    fingerprint: str
    status: TaskStatus
    invocation_id: str | None = None
    events: list[TaskEvent] = field(default_factory=list)
    result: TaskResult | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


class MemoryTaskStore:
    """Concurrency-safe task facts for the standalone in-memory A2A profile."""

    def __init__(self) -> None:
        self._records: dict[str, _StoredTask] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: TaskRequest) -> tuple[TaskSnapshot, bool]:
        fingerprint = task_request_fingerprint(request)
        async with self._lock:
            existing_task_id = self._idempotency.get(request.idempotency_key)
            if existing_task_id is not None:
                existing = self._records[existing_task_id]
                if existing.fingerprint != fingerprint:
                    raise TaskIdempotencyConflict(
                        "a2a.idempotency_conflict",
                        f"idempotency key {request.idempotency_key} has a different request",
                    )
                return _snapshot(existing), False

            if request.task_id in self._records:
                existing = self._records[request.task_id]
                if existing.fingerprint != fingerprint:
                    raise TaskIdempotencyConflict(
                        "a2a.task_id_conflict",
                        f"task id {request.task_id} has a different request",
                    )
                return _snapshot(existing), False

            record = _StoredTask(
                request=request,
                fingerprint=fingerprint,
                status=TaskStatus.SUBMITTED,
            )
            record.events.append(
                TaskEvent(
                    task_id=request.task_id,
                    sequence=1,
                    status=TaskStatus.SUBMITTED,
                )
            )
            self._records[request.task_id] = record
            self._idempotency[request.idempotency_key] = request.task_id
            return _snapshot(record), True

    async def snapshot(self, task_id: str) -> TaskSnapshot:
        record = self._record(task_id)
        async with record.condition:
            return _snapshot(record)

    async def mark_working(self, task_id: str, invocation_id: str) -> TaskSnapshot:
        if not invocation_id.strip():
            raise ValueError("invocation_id must not be empty")
        record = self._record(task_id)
        async with record.condition:
            if record.result is not None:
                raise TaskStateError(
                    "a2a.task_terminal",
                    f"task {task_id} is already terminal",
                )
            if record.invocation_id is not None:
                if record.invocation_id != invocation_id:
                    raise TaskStateError(
                        "a2a.invocation_conflict",
                        f"task {task_id} is already bound to another invocation",
                    )
                return _snapshot(record)
            _ensure_transition(record.status, TaskStatus.WORKING)
            record.invocation_id = invocation_id
            _append(record, TaskStatus.WORKING, {"invocation_id": invocation_id})
            record.condition.notify_all()
            return _snapshot(record)

    async def append_event(
        self,
        task_id: str,
        status: TaskStatus,
        payload: JsonObject,
    ) -> TaskEvent:
        record = self._record(task_id)
        async with record.condition:
            if record.result is not None:
                raise TaskStateError(
                    "a2a.task_terminal",
                    f"task {task_id} is already terminal",
                )
            if status in TERMINAL_TASK_STATUSES:
                raise TaskStateError(
                    "a2a.terminal_requires_finalize",
                    "terminal task status must be written through finalize",
                )
            _ensure_transition(record.status, status)
            event = _append(record, status, payload)
            record.condition.notify_all()
            return event

    async def finalize(self, result: TaskResult) -> TaskSnapshot:
        record = self._record(result.task_id)
        async with record.condition:
            if record.result is not None:
                if record.result != result:
                    raise TaskStateError(
                        "a2a.terminal_conflict",
                        f"task {result.task_id} has a different terminal result",
                    )
                return _snapshot(record)
            if (
                result.invocation_id is not None
                and record.invocation_id is not None
                and result.invocation_id != record.invocation_id
            ):
                raise TaskStateError(
                    "a2a.result_invocation_mismatch",
                    "task result belongs to another invocation",
                )
            _ensure_transition(record.status, result.status)
            if record.invocation_id is None:
                record.invocation_id = result.invocation_id
            payload: JsonObject = {}
            if result.output is not None:
                payload["output"] = result.output
            if result.error_code is not None:
                payload["error_code"] = result.error_code
            if result.error_message is not None:
                payload["error_message"] = result.error_message
            if result.artifacts:
                payload["artifacts"] = [
                    {
                        "artifact_id": artifact.artifact_id,
                        "media_type": artifact.media_type,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "location": artifact.location,
                        "metadata": artifact.metadata,
                    }
                    for artifact in result.artifacts
                ]
            _append(record, result.status, payload)
            record.result = result
            record.condition.notify_all()
            return _snapshot(record)

    async def wait_terminal(self, task_id: str) -> TaskResult:
        record = self._record(task_id)
        async with record.condition:
            while record.result is None:
                await record.condition.wait()
            return record.result

    async def events(
        self,
        task_id: str,
        *,
        start_sequence: int = 1,
    ) -> AsyncIterator[TaskEvent]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        record = self._record(task_id)
        index = start_sequence - 1
        while True:
            async with record.condition:
                while index >= len(record.events) and record.result is None:
                    await record.condition.wait()
                if index < len(record.events):
                    event = record.events[index]
                    index += 1
                else:
                    return
            yield event

    def _record(self, task_id: str) -> _StoredTask:
        try:
            return self._records[task_id]
        except KeyError as exc:
            raise TaskNotFound(
                "a2a.task_not_found",
                f"task {task_id} was not found",
            ) from exc


def _append(record: _StoredTask, status: TaskStatus, payload: JsonObject) -> TaskEvent:
    event = TaskEvent(
        task_id=record.request.task_id,
        sequence=len(record.events) + 1,
        status=status,
        payload=payload,
    )
    record.events.append(event)
    record.status = status
    return event


def _snapshot(record: _StoredTask) -> TaskSnapshot:
    return TaskSnapshot(
        request=record.request,
        fingerprint=record.fingerprint,
        status=record.status,
        invocation_id=record.invocation_id,
        events=tuple(record.events),
        result=record.result,
    )


_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.SUBMITTED: frozenset(
        {
            TaskStatus.WORKING,
            TaskStatus.REJECTED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.RECONCILIATION_REQUIRED,
        }
    ),
    TaskStatus.WORKING: frozenset(
        {
            TaskStatus.WORKING,
            TaskStatus.INPUT_REQUIRED,
            TaskStatus.CANCELLING,
            TaskStatus.REJECTED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.RECONCILIATION_REQUIRED,
        }
    ),
    TaskStatus.INPUT_REQUIRED: frozenset(
        {
            TaskStatus.WORKING,
            TaskStatus.CANCELLING,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.RECONCILIATION_REQUIRED,
        }
    ),
    TaskStatus.CANCELLING: frozenset(
        {
            TaskStatus.CANCELLING,
            TaskStatus.REJECTED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.RECONCILIATION_REQUIRED,
        }
    ),
}


def _ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise TaskStateError(
            "a2a.transition_invalid",
            f"task cannot transition from {current.value} to {target.value}",
        )
