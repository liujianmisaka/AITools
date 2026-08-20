from __future__ import annotations

import asyncio

from misaka_coordinator_runtime import (
    EventDeliveryRecord,
    EventDeliveryStatus,
    EventDeliveryStore,
)
from misaka_kernel_contracts import JsonObject
from misaka_persistence_jsonl import DurableCorruption, JsonlEventLog


class JsonlEventDeliveryStore(EventDeliveryStore):
    """Durable delivery ledger backed by the shared append-only event log."""

    _STREAM = "coordinator.event-delivery"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._records: dict[tuple[str, str], EventDeliveryRecord] = {}
        self._cursors: dict[str, int] = {}
        self._completed: dict[str, set[int]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        async with self._lock:
            await self._open_unlocked()

    async def _open_unlocked(self) -> None:
        if self._loaded:
            return
        for event in await self._log.read(self._STREAM):
            if event.event_type == "delivery.claimed":
                record = _decode_record(event.payload, EventDeliveryStatus.RUNNING)
                self._records[(record.consumer_id, record.event_id)] = record
            elif event.event_type == "delivery.completed":
                record = _decode_record(event.payload, None)
                key = (record.consumer_id, record.event_id)
                current = self._records.get(key)
                if current is None or current.attempts != record.attempts:
                    raise DurableCorruption(
                        "delivery.history_invalid",
                        f"delivery completion has no matching claim for {record.event_id}",
                    )
                self._records[key] = record
                self._advance_cursor(record)
            else:
                raise DurableCorruption(
                    "delivery.event_unknown",
                    f"unknown delivery event {event.event_type}",
                )
        self._loaded = True

    async def get(self, consumer_id: str, event_id: str) -> EventDeliveryRecord | None:
        _require_identity(consumer_id, event_id)
        async with self._lock:
            await self._open_unlocked()
            return self._records.get((consumer_id, event_id))

    async def claim(
        self,
        consumer_id: str,
        event_id: str,
        source_sequence: int,
    ) -> EventDeliveryRecord:
        _require_identity(consumer_id, event_id)
        if source_sequence < 1:
            raise ValueError("source_sequence must be positive")
        async with self._lock:
            await self._open_unlocked()
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
            await self._log.append(
                self._STREAM,
                f"delivery.claim:{consumer_id}:{event_id}:{attempts}",
                "delivery.claimed",
                _encode_record(record),
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
            await self._open_unlocked()
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
            await self._log.append(
                self._STREAM,
                f"delivery.complete:{current.consumer_id}:{current.event_id}:{current.attempts}",
                "delivery.completed",
                _encode_record(completed),
            )
            self._records[key] = completed
            self._advance_cursor(completed)
            return completed

    async def cursor(self, consumer_id: str) -> int:
        if not consumer_id.strip():
            raise ValueError("consumer_id must not be empty")
        async with self._lock:
            await self._open_unlocked()
            return self._cursors.get(consumer_id, 0)

    def _advance_cursor(self, record: EventDeliveryRecord) -> None:
        completed = self._completed.setdefault(record.consumer_id, set())
        completed.add(record.source_sequence)
        cursor = self._cursors.get(record.consumer_id, 0)
        while cursor + 1 in completed:
            cursor += 1
            completed.remove(cursor)
        self._cursors[record.consumer_id] = cursor


def _encode_record(record: EventDeliveryRecord) -> JsonObject:
    payload: JsonObject = {
        "consumer_id": record.consumer_id,
        "event_id": record.event_id,
        "source_sequence": record.source_sequence,
        "status": record.status.value,
        "attempts": record.attempts,
    }
    if record.error_message is not None:
        payload["error_message"] = record.error_message
    return payload


def _decode_record(
    payload: JsonObject,
    expected_status: EventDeliveryStatus | None,
) -> EventDeliveryRecord:
    try:
        status = EventDeliveryStatus(str(payload["status"]))
        if expected_status is not None and status is not expected_status:
            raise ValueError("delivery claim must be running")
        source_sequence = payload["source_sequence"]
        attempts = payload["attempts"]
        if not isinstance(source_sequence, int) or isinstance(source_sequence, bool):
            raise TypeError("source_sequence must be an integer")
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            raise TypeError("attempts must be an integer")
        error = payload.get("error_message")
        if error is not None and not isinstance(error, str):
            raise TypeError("error_message must be a string")
        return EventDeliveryRecord(
            consumer_id=str(payload["consumer_id"]),
            event_id=str(payload["event_id"]),
            source_sequence=source_sequence,
            status=status,
            attempts=attempts,
            error_message=error,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableCorruption(
            "delivery.record_invalid",
            "delivery record has an invalid shape",
        ) from exc


def _require_identity(consumer_id: str, event_id: str) -> None:
    if not consumer_id.strip() or not event_id.strip():
        raise ValueError("consumer and event identities must not be empty")
