from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

from misaka_invocation_contracts import (
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ProviderExecutionRef,
    request_fingerprint,
)
from misaka_kernel_contracts import JsonObject

from misaka_invocation_runtime.errors import IdempotencyConflict, InvocationError


@dataclass(frozen=True, slots=True)
class InvocationSnapshot:
    request: InvocationRequest
    fingerprint: str
    activation_id: str
    status: InvocationStatus
    events: tuple[InvocationEvent, ...]
    result: InvocationResult | None
    provider_execution: ProviderExecutionRef | None = None


class InvocationStore(Protocol):
    async def create(self, request: InvocationRequest) -> tuple[InvocationSnapshot, bool]: ...

    async def snapshot(self, invocation_id: str) -> InvocationSnapshot: ...

    async def append_event(
        self, invocation_id: str, status: InvocationStatus, payload: JsonObject
    ) -> InvocationEvent: ...

    async def finalize(self, result: InvocationResult) -> InvocationSnapshot: ...

    async def wait_terminal(self, invocation_id: str) -> InvocationResult: ...

    def events(
        self, invocation_id: str, *, start_sequence: int = 1
    ) -> AsyncIterator[InvocationEvent]: ...


@dataclass(slots=True)
class _StoredInvocation:
    request: InvocationRequest
    fingerprint: str
    activation_id: str
    status: InvocationStatus
    events: list[InvocationEvent] = field(default_factory=list)
    result: InvocationResult | None = None
    provider_execution: ProviderExecutionRef | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


class MemoryInvocationStore:
    def __init__(self) -> None:
        self._records: dict[str, _StoredInvocation] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        request: InvocationRequest,
    ) -> tuple[InvocationSnapshot, bool]:
        fingerprint = request_fingerprint(request)
        async with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "invocation.idempotency_conflict",
                        f"idempotency key {request.idempotency_key} has a different request",
                    )
                return _snapshot(existing), False
            if request.invocation_id in self._records:
                existing = self._records[request.invocation_id]
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "invocation.id_conflict",
                        f"invocation id {request.invocation_id} has a different request",
                    )
                return _snapshot(existing), False

            record = _StoredInvocation(
                request=request,
                fingerprint=fingerprint,
                activation_id=f"{request.invocation_id}:activation:{request.attempt}",
                status=InvocationStatus.REGISTERED,
            )
            record.events.append(
                InvocationEvent(
                    invocation_id=request.invocation_id,
                    sequence=1,
                    status=InvocationStatus.REGISTERED,
                )
            )
            self._records[request.invocation_id] = record
            self._idempotency[request.idempotency_key] = request.invocation_id
            return _snapshot(record), True

    async def snapshot(self, invocation_id: str) -> InvocationSnapshot:
        record = self._record(invocation_id)
        async with record.condition:
            return _snapshot(record)

    async def append_event(
        self,
        invocation_id: str,
        status: InvocationStatus,
        payload: JsonObject,
    ) -> InvocationEvent:
        record = self._record(invocation_id)
        async with record.condition:
            if record.result is not None:
                raise InvocationError(
                    "invocation.already_terminal",
                    f"invocation {invocation_id} is already terminal",
                )
            if status in _TERMINAL_STATUSES:
                raise InvocationError(
                    "invocation.terminal_requires_finalize",
                    "terminal invocation status must be written through finalize",
                )
            _ensure_transition(record.status, status)
            provider_execution = _merge_provider_execution(
                record.provider_execution,
                payload,
            )
            event = InvocationEvent(
                invocation_id=invocation_id,
                sequence=len(record.events) + 1,
                status=status,
                payload=payload,
            )
            record.events.append(event)
            record.status = status
            record.provider_execution = provider_execution
            record.condition.notify_all()
            return event

    async def finalize(self, result: InvocationResult) -> InvocationSnapshot:
        record = self._record(result.invocation_id)
        async with record.condition:
            if record.result is not None:
                if record.result != result:
                    raise InvocationError(
                        "invocation.terminal_conflict",
                        f"invocation {result.invocation_id} has a different terminal result",
                    )
                return _snapshot(record)
            _ensure_transition(record.status, result.status)
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
            event = InvocationEvent(
                invocation_id=result.invocation_id,
                sequence=len(record.events) + 1,
                status=result.status,
                payload=payload,
            )
            record.events.append(event)
            record.status = result.status
            record.result = result
            record.condition.notify_all()
            return _snapshot(record)

    async def wait_terminal(self, invocation_id: str) -> InvocationResult:
        record = self._record(invocation_id)
        async with record.condition:
            while record.result is None:
                await record.condition.wait()
            return record.result

    async def events(
        self, invocation_id: str, *, start_sequence: int = 1
    ) -> AsyncIterator[InvocationEvent]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        record = self._record(invocation_id)
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

    def _record(self, invocation_id: str) -> _StoredInvocation:
        try:
            return self._records[invocation_id]
        except KeyError as exc:
            raise InvocationError(
                "invocation.not_found",
                f"invocation {invocation_id} was not found",
            ) from exc


def _snapshot(record: _StoredInvocation) -> InvocationSnapshot:
    return InvocationSnapshot(
        request=record.request,
        fingerprint=record.fingerprint,
        activation_id=record.activation_id,
        status=record.status,
        events=tuple(record.events),
        result=record.result,
        provider_execution=record.provider_execution,
    )


def _merge_provider_execution(
    current: ProviderExecutionRef | None,
    payload: JsonObject,
) -> ProviderExecutionRef | None:
    provider_id = _optional_string(payload.get("provider_id"), "provider_id")
    epoch = _optional_positive_int(payload.get("provider_epoch"), "provider_epoch")
    session_id = _optional_string(payload.get("provider_session_id"), "provider_session_id")
    operation_id = _optional_string(
        payload.get("provider_operation_id"),
        "provider_operation_id",
    )
    external_start_attempted = payload.get("external_start_attempted")
    if external_start_attempted is not None and not isinstance(external_start_attempted, bool):
        raise InvocationError(
            "invocation.provider_fact_invalid",
            "external_start_attempted must be a boolean",
        )
    if current is None:
        if all(
            value is None
            for value in (provider_id, epoch, session_id, operation_id, external_start_attempted)
        ):
            return None
        if provider_id is None or epoch is None:
            raise InvocationError(
                "invocation.provider_binding_incomplete",
                "provider facts require provider_id and provider_epoch",
            )
        return ProviderExecutionRef(
            provider_id=provider_id,
            provider_epoch=epoch,
            provider_session_id=session_id,
            provider_operation_id=operation_id,
            external_start_attempted=external_start_attempted is True,
        )
    if provider_id is not None and provider_id != current.provider_id:
        raise InvocationError(
            "invocation.provider_binding_conflict",
            "invocation provider_id cannot change",
        )
    if epoch is not None and epoch != current.provider_epoch:
        raise InvocationError(
            "invocation.provider_epoch_conflict",
            "invocation provider_epoch cannot change",
        )
    if (
        session_id is not None
        and current.provider_session_id is not None
        and session_id != current.provider_session_id
    ):
        raise InvocationError(
            "invocation.provider_session_conflict",
            "invocation provider_session_id cannot change",
        )
    if (
        operation_id is not None
        and current.provider_operation_id is not None
        and operation_id != current.provider_operation_id
    ):
        raise InvocationError(
            "invocation.provider_operation_conflict",
            "invocation provider_operation_id cannot change",
        )
    return ProviderExecutionRef(
        provider_id=current.provider_id,
        provider_epoch=current.provider_epoch,
        provider_session_id=session_id or current.provider_session_id,
        provider_operation_id=operation_id or current.provider_operation_id,
        external_start_attempted=(
            current.external_start_attempted or external_start_attempted is True
        ),
    )


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvocationError(
            "invocation.provider_fact_invalid",
            f"{field_name} must be a non-empty string",
        )
    return value.strip()


def _optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvocationError(
            "invocation.provider_fact_invalid",
            f"{field_name} must be a positive integer",
        )
    return value


_TERMINAL_STATUSES = frozenset(
    {
        InvocationStatus.REJECTED,
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
        InvocationStatus.CANCELLED,
        InvocationStatus.RECONCILIATION_REQUIRED,
    }
)

_ALLOWED_TRANSITIONS: dict[InvocationStatus, frozenset[InvocationStatus]] = {
    InvocationStatus.REGISTERED: frozenset(
        {
            InvocationStatus.PREFLIGHTING,
            InvocationStatus.REJECTED,
            InvocationStatus.CANCELLED,
            InvocationStatus.FAILED,
            InvocationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    InvocationStatus.PREFLIGHTING: frozenset(
        {
            InvocationStatus.RESOURCE_ACQUIRING,
            InvocationStatus.PREPARED,
            InvocationStatus.STARTING,
            InvocationStatus.REJECTED,
            InvocationStatus.CANCELLED,
            InvocationStatus.FAILED,
            InvocationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    InvocationStatus.RESOURCE_ACQUIRING: frozenset(
        {
            InvocationStatus.PREPARED,
            InvocationStatus.CANCELLED,
            InvocationStatus.FAILED,
            InvocationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    InvocationStatus.PREPARED: frozenset(
        {
            InvocationStatus.STARTING,
            InvocationStatus.CANCELLED,
            InvocationStatus.FAILED,
            InvocationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    InvocationStatus.STARTING: frozenset(
        {
            InvocationStatus.RUNNING,
            InvocationStatus.STOPPING,
            InvocationStatus.FAILED,
            InvocationStatus.CANCELLED,
            InvocationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    InvocationStatus.RUNNING: frozenset(
        {
            InvocationStatus.RUNNING,
            InvocationStatus.STOPPING,
            InvocationStatus.FINALIZING,
            InvocationStatus.FAILED,
            InvocationStatus.CANCELLED,
            InvocationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    InvocationStatus.STOPPING: frozenset(
        {
            InvocationStatus.STOPPING,
            InvocationStatus.FINALIZING,
            InvocationStatus.FAILED,
            InvocationStatus.CANCELLED,
            InvocationStatus.RECONCILIATION_REQUIRED,
        }
    ),
    InvocationStatus.FINALIZING: _TERMINAL_STATUSES,
}


def _ensure_transition(current: InvocationStatus, target: InvocationStatus) -> None:
    if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvocationError(
            "invocation.transition_invalid",
            f"invocation cannot transition from {current.value} to {target.value}",
        )
