from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from misaka_kernel_contracts import JsonObject

from misaka_persistence_jsonl.contracts import DurableEvent
from misaka_persistence_jsonl.errors import DurableConflict, DurableCorruption


class JsonlEventLog:
    """Append-only JSONL log with per-stream sequence and event-id idempotency."""

    def __init__(self, path: str | Path, *, fsync: bool = True) -> None:
        self.path = Path(path)
        self._fsync = fsync
        self._lock = asyncio.Lock()
        self._loaded = False
        self._events: list[DurableEvent] = []
        self._by_stream: dict[str, list[DurableEvent]] = {}
        self._by_key: dict[tuple[str, str], DurableEvent] = {}

    async def open(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                lines = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
                try:
                    decoded = [json.loads(line) for line in lines.splitlines() if line.strip()]
                except json.JSONDecodeError as exc:
                    raise DurableCorruption(
                        "durable.invalid_json", "JSONL event log contains invalid JSON"
                    ) from exc
                for item in decoded:
                    if not isinstance(item, dict):
                        raise DurableCorruption(
                            "durable.invalid_record", "JSONL event record must be an object"
                        )
                    self._index(self._decode(cast(dict[str, object], item)))
            self._loaded = True

    async def append(
        self,
        stream_id: str,
        event_id: str,
        event_type: str,
        payload: JsonObject,
        *,
        occurred_at: datetime | None = None,
    ) -> DurableEvent:
        await self.open()
        async with self._lock:
            existing = self._by_key.get((stream_id, event_id))
            if existing is not None:
                if existing.event_type != event_type or existing.payload != payload:
                    raise DurableConflict(
                        "durable.event_conflict",
                        f"event {event_id} already exists with different content",
                    )
                return existing
            sequence = len(self._by_stream.get(stream_id, ())) + 1
            event = DurableEvent(
                stream_id=stream_id,
                sequence=sequence,
                event_id=event_id,
                event_type=event_type,
                payload=payload,
                occurred_at=occurred_at or datetime.now(UTC),
            )
            line = json.dumps(_encode(event), ensure_ascii=False, sort_keys=True) + "\n"
            await asyncio.to_thread(self._append_line, line)
            self._index(event)
            return event

    async def read(self, stream_id: str, *, start_sequence: int = 1) -> tuple[DurableEvent, ...]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        await self.open()
        async with self._lock:
            return tuple(self._by_stream.get(stream_id, ())[start_sequence - 1 :])

    async def all_events(self) -> tuple[DurableEvent, ...]:
        await self.open()
        async with self._lock:
            return tuple(self._events)

    async def close(self) -> None:
        async with self._lock:
            self._loaded = False
            self._events.clear()
            self._by_stream.clear()
            self._by_key.clear()

    def _append_line(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())

    def _index(self, event: DurableEvent) -> None:
        key = (event.stream_id, event.event_id)
        if key in self._by_key:
            raise DurableCorruption(
                "durable.duplicate_event", f"duplicate event {event.event_id} in stream"
            )
        stream = self._by_stream.setdefault(event.stream_id, [])
        expected = len(stream) + 1
        if event.sequence != expected:
            raise DurableCorruption(
                "durable.sequence_gap",
                f"stream {event.stream_id} expected sequence {expected}, got {event.sequence}",
            )
        stream.append(event)
        self._events.append(event)
        self._by_key[key] = event

    @staticmethod
    def _decode(item: dict[str, object]) -> DurableEvent:
        try:
            occurred_at = datetime.fromisoformat(str(item["occurred_at"]))
            payload = item["payload"]
            if not isinstance(payload, dict):
                raise TypeError("payload must be an object")
            sequence_value = item["sequence"]
            if isinstance(sequence_value, bool) or not isinstance(sequence_value, (int, str)):
                raise TypeError("sequence must be an integer")
            return DurableEvent(
                stream_id=str(item["stream_id"]),
                sequence=int(sequence_value),
                event_id=str(item["event_id"]),
                event_type=str(item["event_type"]),
                payload=cast(JsonObject, payload),
                occurred_at=occurred_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DurableCorruption(
                "durable.invalid_record", "JSONL event record has an invalid shape"
            ) from exc


def _encode(event: DurableEvent) -> JsonObject:
    return {
        "stream_id": event.stream_id,
        "sequence": event.sequence,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "payload": event.payload,
        "occurred_at": event.occurred_at.isoformat(),
    }
