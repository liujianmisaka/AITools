from __future__ import annotations

import asyncio

from misaka_coordinator_runtime.contracts import EventDeliveryRecord, EventDeliveryStatus


class MemoryEventDeliveryStore:
    """At-least-once delivery ledger with a contiguous replay cursor."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], EventDeliveryRecord] = {}
        self._completed: dict[str, set[int]] = {}
        self._cursors: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def get(self, consumer_id: str, event_id: str) -> EventDeliveryRecord | None:
        _require_consumer(consumer_id)
        if not event_id.strip():
            raise ValueError("event_id must not be empty")
        async with self._lock:
            return self._records.get((consumer_id, event_id))

    async def claim(
        self, consumer_id: str, event_id: str, source_sequence: int
    ) -> EventDeliveryRecord:
        _require_consumer(consumer_id)
        if not event_id.strip() or source_sequence < 1:
            raise ValueError("event identity and source sequence must be valid")
        async with self._lock:
            key = (consumer_id, event_id)
            existing = self._records.get(key)
            if existing is not None and existing.status is not EventDeliveryStatus.RUNNING:
                return existing
            attempts = (existing.attempts if existing is not None else 0) + 1
            record = EventDeliveryRecord(
                consumer_id=consumer_id,
                event_id=event_id,
                source_sequence=source_sequence,
                status=EventDeliveryStatus.RUNNING,
                attempts=attempts,
            )
            self._records[key] = record
            return record

    async def complete(
        self,
        record: EventDeliveryRecord,
        *,
        status: EventDeliveryStatus,
        error_message: str | None = None,
    ) -> EventDeliveryRecord:
        if status is EventDeliveryStatus.RUNNING:
            raise ValueError("completion status must be terminal")
        async with self._lock:
            key = (record.consumer_id, record.event_id)
            current = self._records.get(key)
            if current is None:
                raise KeyError(record.event_id)
            if current.status is not EventDeliveryStatus.RUNNING:
                return current
            completed = EventDeliveryRecord(
                consumer_id=current.consumer_id,
                event_id=current.event_id,
                source_sequence=current.source_sequence,
                status=status,
                attempts=current.attempts,
                error_message=error_message,
            )
            self._records[key] = completed
            completed_sequences = self._completed.setdefault(current.consumer_id, set())
            completed_sequences.add(current.source_sequence)
            cursor = self._cursors.get(current.consumer_id, 0)
            while cursor + 1 in completed_sequences:
                cursor += 1
                completed_sequences.remove(cursor)
            self._cursors[current.consumer_id] = cursor
            return completed

    async def cursor(self, consumer_id: str) -> int:
        _require_consumer(consumer_id)
        async with self._lock:
            return self._cursors.get(consumer_id, 0)


def _require_consumer(consumer_id: str) -> None:
    if not consumer_id.strip():
        raise ValueError("consumer_id must not be empty")
