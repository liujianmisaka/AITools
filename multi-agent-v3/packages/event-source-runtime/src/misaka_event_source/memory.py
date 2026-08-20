from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

from misaka_kernel_contracts import JsonObject

from misaka_event_source.contracts import CloudEvent


class MemoryEventSource:
    """Replayable, bounded-lifetime source used by adapters and contract tests."""

    def __init__(self, *, max_events: int = 10_000) -> None:
        if max_events < 1:
            raise ValueError("max_events must be at least one")
        self._max_events = max_events
        self._events: list[CloudEvent] = []
        self._event_ids: set[str] = set()
        self._condition = asyncio.Condition()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_sequence(self) -> int:
        return len(self._events)

    async def publish(self, event: CloudEvent) -> CloudEvent:
        async with self._condition:
            if self._closed:
                raise RuntimeError("event source is closed")
            if event.event_id in self._event_ids:
                return next(item for item in self._events if item.event_id == event.event_id)
            if len(self._events) >= self._max_events:
                raise RuntimeError("event source capacity exceeded")
            event = replace(event, sequence=len(self._events) + 1)
            self._events.append(event)
            self._event_ids.add(event.event_id)
            self._condition.notify_all()
            return event

    async def publish_data(
        self,
        *,
        event_id: str,
        source: str,
        event_type: str,
        data: JsonObject,
        subject: str | None = None,
    ) -> CloudEvent:
        return await self.publish(
            CloudEvent(
                event_id=event_id,
                source=source,
                event_type=event_type,
                data=data,
                subject=subject,
            )
        )

    async def events(self, *, start_sequence: int = 1) -> AsyncIterator[CloudEvent]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        index = start_sequence - 1
        while True:
            async with self._condition:
                while index >= len(self._events) and not self._closed:
                    await self._condition.wait()
                if index >= len(self._events):
                    return
                event = self._events[index]
                index += 1
            yield event

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
