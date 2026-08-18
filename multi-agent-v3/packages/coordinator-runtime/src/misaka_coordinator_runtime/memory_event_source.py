from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from misaka_kernel_contracts import JsonObject

from misaka_coordinator_runtime.contracts import EventEnvelope
from misaka_coordinator_runtime.errors import CoordinatorStateError


class MemoryEventSource:
    """A bounded-lifetime in-memory event source for local coordinators."""

    def __init__(self) -> None:
        self._events: list[EventEnvelope] = []
        self._event_ids: set[str] = set()
        self._condition = asyncio.Condition()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_sequence(self) -> int:
        return len(self._events)

    async def publish(
        self,
        topic: str,
        payload: JsonObject,
        *,
        event_id: str,
    ) -> EventEnvelope:
        if not topic.strip() or not event_id.strip():
            raise ValueError("topic and event_id must not be empty")
        async with self._condition:
            if self._closed:
                raise CoordinatorStateError("event_source.closed", "event source is closed")
            for event in self._events:
                if event.event_id == event_id:
                    return event
            event = EventEnvelope(
                sequence=len(self._events) + 1,
                event_id=event_id,
                topic=topic,
                payload=payload,
            )
            self._events.append(event)
            self._event_ids.add(event_id)
            self._condition.notify_all()
            return event

    async def events(
        self,
        *,
        start_sequence: int = 1,
        topic: str | None = None,
    ) -> AsyncIterator[EventEnvelope]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        if topic is not None and not topic.strip():
            raise ValueError("topic must not be empty when provided")
        index = start_sequence - 1
        while True:
            async with self._condition:
                while index >= len(self._events) and not self._closed:
                    await self._condition.wait()
                if index >= len(self._events):
                    return
                event = self._events[index]
                index += 1
            if topic is None or event.topic == topic:
                yield event

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
