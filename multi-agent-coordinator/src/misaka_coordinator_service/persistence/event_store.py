from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from misaka_coordinator_service.domain._serialization import (
    datetime_to_text,
    ensure_text,
    ensure_utc,
    read_datetime,
    read_int,
    read_mapping,
    read_text,
)
from misaka_coordinator_service.execution import JsonObject, JsonValue

EVENT_RECORD_SCHEMA_VERSION = 1


class CoordinatorEventStoreError(RuntimeError):
    """Raised when Coordinator session events cannot be persisted or read."""


@dataclass(frozen=True, slots=True)
class CoordinatorSessionEvent:
    session_id: str
    sequence: int
    event_id: str
    event_type: str
    payload: JsonObject
    occurred_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("session_id", "event_id", "event_type"):
            object.__setattr__(self, field_name, ensure_text(getattr(self, field_name), field_name))
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise CoordinatorEventStoreError("event sequence must be positive")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at, "occurred_at"))
        _validate_json_object(cast(Mapping[object, object], self.payload), "payload")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EVENT_RECORD_SCHEMA_VERSION,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "occurred_at": datetime_to_text(self.occurred_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CoordinatorSessionEvent:
        if read_int(value, "schema_version") != EVENT_RECORD_SCHEMA_VERSION:
            raise CoordinatorEventStoreError("unsupported Coordinator event record version")
        payload = read_mapping(value, "payload")
        try:
            return cls(
                session_id=read_text(value, "session_id"),
                sequence=read_int(value, "sequence"),
                event_id=read_text(value, "event_id"),
                event_type=read_text(value, "event_type"),
                payload=cast(JsonObject, payload),
                occurred_at=read_datetime(value, "occurred_at"),
            )
        except (TypeError, ValueError, CoordinatorEventStoreError) as error:
            raise CoordinatorEventStoreError("Coordinator event record is invalid") from error


class CoordinatorEventStorePort(Protocol):
    def list_events(
        self, session_id: str, *, next_sequence: int = 1
    ) -> tuple[CoordinatorSessionEvent, ...]: ...

    def append(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, JsonValue],
        *,
        occurred_at: datetime | None = None,
    ) -> CoordinatorSessionEvent: ...

    def stream_events(
        self, session_id: str, *, next_sequence: int = 1
    ) -> AsyncIterator[CoordinatorSessionEvent]: ...

    def close(self) -> None: ...


class JsonlCoordinatorEventStore:
    """Durable append-only Coordinator event log with cursor-based subscriptions."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = None if path is None else Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self._events: dict[str, list[CoordinatorSessionEvent]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[CoordinatorSessionEvent]]] = {}
        if self.path is not None:
            self._load()

    def list_events(
        self, session_id: str, *, next_sequence: int = 1
    ) -> tuple[CoordinatorSessionEvent, ...]:
        normalized_session_id = ensure_text(session_id, "session_id")
        if isinstance(next_sequence, bool) or next_sequence < 1:
            raise CoordinatorEventStoreError("next_sequence must be positive")
        with self._lock:
            return tuple(
                event
                for event in self._events.get(normalized_session_id, ())
                if event.sequence >= next_sequence
            )

    def append(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, JsonValue],
        *,
        occurred_at: datetime | None = None,
    ) -> CoordinatorSessionEvent:
        normalized_session_id = ensure_text(session_id, "session_id")
        normalized_event_type = ensure_text(event_type, "event_type")
        normalized_payload = dict(payload)
        _validate_json_object(cast(Mapping[object, object], normalized_payload), "payload")
        at = datetime.now(UTC) if occurred_at is None else ensure_utc(occurred_at, "occurred_at")
        with self._lock:
            events = self._events.setdefault(normalized_session_id, [])
            event = CoordinatorSessionEvent(
                session_id=normalized_session_id,
                sequence=len(events) + 1,
                event_id=f"coordinator:{normalized_session_id}:{len(events) + 1}:{uuid4().hex}",
                event_type=normalized_event_type,
                payload=normalized_payload,
                occurred_at=at,
            )
            if self.path is not None:
                self._append_line(event)
            events.append(event)
            for queue in tuple(self._subscribers.get(normalized_session_id, ())):
                queue.put_nowait(event)
            return event

    async def stream_events(
        self, session_id: str, *, next_sequence: int = 1
    ) -> AsyncIterator[CoordinatorSessionEvent]:
        normalized_session_id = ensure_text(session_id, "session_id")
        if isinstance(next_sequence, bool) or next_sequence < 1:
            raise CoordinatorEventStoreError("next_sequence must be positive")
        queue: asyncio.Queue[CoordinatorSessionEvent] = asyncio.Queue()
        with self._lock:
            replay = tuple(
                event
                for event in self._events.get(normalized_session_id, ())
                if event.sequence >= next_sequence
            )
            self._subscribers.setdefault(normalized_session_id, set()).add(queue)
        try:
            for event in replay:
                yield event
            cursor = (replay[-1].sequence + 1) if replay else next_sequence
            while True:
                event = await queue.get()
                if event.sequence < cursor:
                    continue
                cursor = event.sequence + 1
                yield event
        finally:
            with self._lock:
                subscribers = self._subscribers.get(normalized_session_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(normalized_session_id, None)

    def close(self) -> None:
        with self._lock:
            self._subscribers.clear()

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        decoded = cast(object, json.loads(line))
                    except json.JSONDecodeError as error:
                        raise CoordinatorEventStoreError(
                            f"event store line {line_number} is not valid JSON"
                        ) from error
                    if not isinstance(decoded, dict):
                        raise CoordinatorEventStoreError(
                            f"event store line {line_number} must be an object"
                        )
                    raw = cast(dict[object, object], decoded)
                    if any(not isinstance(key, str) for key in raw):
                        raise CoordinatorEventStoreError(
                            f"event store line {line_number} keys must be strings"
                        )
                    event = CoordinatorSessionEvent.from_dict(cast(dict[str, object], raw))
                    events = self._events.setdefault(event.session_id, [])
                    if event.sequence != len(events) + 1:
                        raise CoordinatorEventStoreError(
                            f"event store line {line_number} has a non-contiguous sequence"
                        )
                    events.append(event)
        except OSError as error:
            raise CoordinatorEventStoreError("failed to read Coordinator event store") from error

    def _append_line(self, event: CoordinatorSessionEvent) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise CoordinatorEventStoreError("failed to append Coordinator event") from error


def _validate_json_object(value: Mapping[object, object], field_name: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise CoordinatorEventStoreError(f"{field_name} keys must be strings")
    for item in value.values():
        _validate_json_value(item, field_name)


def _validate_json_value(value: object, field_name: str) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in cast(list[object], value):
            _validate_json_value(item, field_name)
        return
    if isinstance(value, dict):
        _validate_json_object(cast(Mapping[object, object], value), field_name)
        return
    raise CoordinatorEventStoreError(f"{field_name} must contain JSON values")
