from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableEvent


class DelegationSessionEventKind(StrEnum):
    LIFECYCLE = "lifecycle"
    OUTPUT_DELTA = "output_delta"
    OUTPUT_COMPLETED = "output_completed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    TERMINAL = "terminal"
    SESSION_CLOSED = "session_closed"


@dataclass(frozen=True, slots=True)
class DelegationSessionEvent:
    """Public, provider-neutral observation of a delegated Agent session."""

    delegation_id: str
    sequence: int
    kind: DelegationSessionEventKind
    invocation_id: str | None = None
    activation_id: str | None = None
    activation_number: int | None = None
    status: str | None = None
    provider_session_id: str | None = None
    provider_operation_id: str | None = None
    payload: JsonObject = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.delegation_id.strip():
            raise ValueError("delegation_id must not be empty")
        if self.sequence < 1:
            raise ValueError("session event sequence must be at least one")
        for field_name, value in {
            "invocation_id": self.invocation_id,
            "activation_id": self.activation_id,
            "status": self.status,
            "provider_session_id": self.provider_session_id,
            "provider_operation_id": self.provider_operation_id,
        }.items():
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} must not be empty when provided")
        if (self.invocation_id is None) != (self.activation_id is None):
            raise ValueError("invocation_id and activation_id must be provided together")
        if self.activation_number is not None and self.activation_number < 1:
            raise ValueError("activation_number must be at least one when provided")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DelegationSessionEventInspection:
    delegation_id: str
    last_sequence: int = 0
    closed: bool = False
    last_occurred_at: datetime | None = None
    provider_session_id: str | None = None
    provider_operation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.delegation_id.strip():
            raise ValueError("delegation_id must not be empty")
        if self.last_sequence < 0:
            raise ValueError("last_sequence must not be negative")
        if self.last_occurred_at is not None and (
            self.last_occurred_at.tzinfo is None or self.last_occurred_at.utcoffset() is None
        ):
            raise ValueError("last_occurred_at must be timezone-aware")


class DelegationSessionEventSink(Protocol):
    async def publish(
        self,
        *,
        delegation_id: str,
        event_id: str,
        kind: DelegationSessionEventKind,
        invocation_id: str | None = None,
        activation_id: str | None = None,
        activation_number: int | None = None,
        status: str | None = None,
        provider_session_id: str | None = None,
        provider_operation_id: str | None = None,
        payload: JsonObject | None = None,
        occurred_at: datetime | None = None,
    ) -> DelegationSessionEvent: ...

    async def close_session(
        self,
        delegation_id: str,
        *,
        event_id: str,
        invocation_id: str | None = None,
        activation_id: str | None = None,
        activation_number: int | None = None,
        status: str | None = None,
        provider_session_id: str | None = None,
        provider_operation_id: str | None = None,
    ) -> DelegationSessionEvent: ...

class _DurableEventLog(Protocol):
    async def read(
        self, stream_id: str, *, start_sequence: int = 1
    ) -> tuple[DurableEvent, ...]: ...

    async def append(
        self,
        stream_id: str,
        event_id: str,
        event_type: str,
        payload: JsonObject,
        *,
        schema_version: int = 1,
        occurred_at: datetime | None = None,
    ) -> DurableEvent: ...


@dataclass(slots=True)
class _EventRecord:
    events: list[DelegationSessionEvent] = field(default_factory=list)
    event_ids: dict[str, DelegationSessionEvent] = field(default_factory=dict)
    loaded: bool = False
    closed: bool = False
    provider_session_id: str | None = None
    provider_operation_id: str | None = None


class DelegationSessionEventStore(DelegationSessionEventSink):
    """Ordered in-process observation hub with an optional durable JSONL backend."""

    _STREAM_PREFIX = "delegation.session.events:"
    _EVENT_TYPE = "delegation.session.event"

    def __init__(self, log: _DurableEventLog | None = None) -> None:
        self._log = log
        self._records: dict[str, _EventRecord] = {}
        self._condition = asyncio.Condition()
        self._closed = False

    async def open(self) -> None:
        async with self._condition:
            if self._closed:
                self._records.clear()
                self._closed = False

    async def publish(
        self,
        *,
        delegation_id: str,
        event_id: str,
        kind: DelegationSessionEventKind,
        invocation_id: str | None = None,
        activation_id: str | None = None,
        activation_number: int | None = None,
        status: str | None = None,
        provider_session_id: str | None = None,
        provider_operation_id: str | None = None,
        payload: JsonObject | None = None,
        occurred_at: datetime | None = None,
    ) -> DelegationSessionEvent:
        _validate_id(delegation_id, "delegation_id")
        _validate_id(event_id, "event_id")
        async with self._condition:
            if self._closed:
                raise RuntimeError("delegation session event store is closed")
            record = await self._record_locked(delegation_id)
            existing = record.event_ids.get(event_id)
            if existing is not None:
                if not _same_event(
                    existing,
                    kind=kind,
                    invocation_id=invocation_id,
                    activation_id=activation_id,
                    activation_number=activation_number,
                    status=status,
                    provider_session_id=provider_session_id,
                    provider_operation_id=provider_operation_id,
                    payload=payload or {},
                ):
                    raise ValueError(f"session event {event_id} conflicts with existing content")
                return existing
            event = DelegationSessionEvent(
                delegation_id=delegation_id,
                sequence=len(record.events) + 1,
                kind=kind,
                invocation_id=invocation_id,
                activation_id=activation_id,
                activation_number=activation_number,
                status=status,
                provider_session_id=provider_session_id,
                provider_operation_id=provider_operation_id,
                payload=dict(payload or {}),
                occurred_at=occurred_at or datetime.now(UTC),
            )
            if self._log is not None:
                durable = await self._log.append(
                    self._stream(delegation_id),
                    event_id,
                    self._EVENT_TYPE,
                    _encode_event(event),
                    occurred_at=event.occurred_at,
                )
                event = _decode_event(durable)
                if event.sequence != len(record.events) + 1:
                    raise ValueError("durable session event sequence is not contiguous")
            record.events.append(event)
            record.event_ids[event_id] = event
            if event.provider_session_id is not None:
                record.provider_session_id = event.provider_session_id
            if event.provider_operation_id is not None:
                record.provider_operation_id = event.provider_operation_id
            if event.kind is DelegationSessionEventKind.SESSION_CLOSED:
                record.closed = True
            self._condition.notify_all()
            return event

    async def close_session(
        self,
        delegation_id: str,
        *,
        event_id: str,
        invocation_id: str | None = None,
        activation_id: str | None = None,
        activation_number: int | None = None,
        status: str | None = None,
        provider_session_id: str | None = None,
        provider_operation_id: str | None = None,
    ) -> DelegationSessionEvent:
        return await self.publish(
            delegation_id=delegation_id,
            event_id=event_id,
            kind=DelegationSessionEventKind.SESSION_CLOSED,
            invocation_id=invocation_id,
            activation_id=activation_id,
            activation_number=activation_number,
            status=status,
            provider_session_id=provider_session_id,
            provider_operation_id=provider_operation_id,
            payload={"reason": "session_closed"},
        )

    async def read(
        self, delegation_id: str, *, start_sequence: int = 1
    ) -> tuple[DelegationSessionEvent, ...]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        _validate_id(delegation_id, "delegation_id")
        async with self._condition:
            record = await self._record_locked(delegation_id)
            return tuple(record.events[start_sequence - 1 :])

    async def events(
        self, delegation_id: str, *, start_sequence: int = 1
    ) -> AsyncIterator[DelegationSessionEvent]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        _validate_id(delegation_id, "delegation_id")
        index = start_sequence - 1
        while True:
            async with self._condition:
                if self._closed:
                    return
                record = await self._record_locked(delegation_id)
                while index >= len(record.events) and not record.closed:
                    await self._condition.wait()
                    record = await self._record_locked(delegation_id)
                if index >= len(record.events):
                    return
                event = record.events[index]
                index += 1
            yield event

    async def inspect(self, delegation_id: str) -> DelegationSessionEventInspection:
        _validate_id(delegation_id, "delegation_id")
        async with self._condition:
            record = await self._record_locked(delegation_id)
            last = record.events[-1] if record.events else None
            return DelegationSessionEventInspection(
                delegation_id=delegation_id,
                last_sequence=len(record.events),
                closed=record.closed,
                last_occurred_at=last.occurred_at if last is not None else None,
                provider_session_id=record.provider_session_id,
                provider_operation_id=record.provider_operation_id,
            )

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            for record in self._records.values():
                record.closed = True
            self._condition.notify_all()

    async def _record_locked(self, delegation_id: str) -> _EventRecord:
        record = self._records.setdefault(delegation_id, _EventRecord())
        if record.loaded or self._log is None:
            record.loaded = True
            return record
        durable_events = await self._log.read(self._stream(delegation_id))
        for durable in durable_events:
            event = _decode_event(durable)
            if event.delegation_id != delegation_id:
                raise ValueError("durable session event delegation id does not match stream")
            if event.sequence != len(record.events) + 1:
                raise ValueError("durable session event sequence is not contiguous")
            record.events.append(event)
            record.event_ids[durable.event_id] = event
            if event.provider_session_id is not None:
                record.provider_session_id = event.provider_session_id
            if event.provider_operation_id is not None:
                record.provider_operation_id = event.provider_operation_id
            if event.kind is DelegationSessionEventKind.SESSION_CLOSED:
                record.closed = True
        record.loaded = True
        return record

    @classmethod
    def _stream(cls, delegation_id: str) -> str:
        return f"{cls._STREAM_PREFIX}{_validate_id(delegation_id, 'delegation_id')}"


def _encode_event(event: DelegationSessionEvent) -> JsonObject:
    return {
        "delegation_id": event.delegation_id,
        "sequence": event.sequence,
        "kind": event.kind.value,
        "invocation_id": event.invocation_id,
        "activation_id": event.activation_id,
        "activation_number": event.activation_number,
        "status": event.status,
        "provider_session_id": event.provider_session_id,
        "provider_operation_id": event.provider_operation_id,
        "payload": event.payload,
        "occurred_at": event.occurred_at.isoformat(),
    }


def _decode_event(event: DurableEvent) -> DelegationSessionEvent:
    payload = event.payload
    sequence = _required_int(payload, "sequence")
    if sequence != event.sequence:
        raise ValueError("durable session event sequence does not match stream sequence")
    kind_value = payload.get("kind")
    if not isinstance(kind_value, str):
        raise ValueError("durable session event kind must be a string")
    try:
        kind = DelegationSessionEventKind(kind_value)
    except ValueError as exc:
        raise ValueError(f"unsupported session event kind: {kind_value}") from exc
    event_payload = payload.get("payload", {})
    if not isinstance(event_payload, dict):
        raise ValueError("durable session event payload must be an object")
    occurred_at = payload.get("occurred_at")
    if not isinstance(occurred_at, str):
        raise ValueError("durable session event occurred_at must be a string")
    return DelegationSessionEvent(
        delegation_id=_required_string(payload, "delegation_id"),
        sequence=sequence,
        kind=kind,
        invocation_id=_optional_string(payload.get("invocation_id")),
        activation_id=_optional_string(payload.get("activation_id")),
        activation_number=_optional_int(payload.get("activation_number")),
        status=_optional_string(payload.get("status")),
        provider_session_id=_optional_string(payload.get("provider_session_id")),
        provider_operation_id=_optional_string(payload.get("provider_operation_id")),
        payload=cast(JsonObject, event_payload),
        occurred_at=datetime.fromisoformat(occurred_at),
    )


def _validate_id(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _required_string(payload: JsonObject, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("optional session event string is invalid")
    return value


def _required_int(payload: JsonObject, name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("optional session event integer is invalid")
    return value


def _same_event(
    event: DelegationSessionEvent,
    *,
    kind: DelegationSessionEventKind,
    invocation_id: str | None,
    activation_id: str | None,
    activation_number: int | None,
    status: str | None,
    provider_session_id: str | None,
    provider_operation_id: str | None,
    payload: JsonObject,
) -> bool:
    return (
        event.kind is kind
        and event.invocation_id == invocation_id
        and event.activation_id == activation_id
        and event.activation_number == activation_number
        and event.status == status
        and event.provider_session_id == provider_session_id
        and event.provider_operation_id == provider_operation_id
        and event.payload == payload
    )
