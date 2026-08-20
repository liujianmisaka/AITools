from __future__ import annotations

from collections.abc import AsyncIterator

from misaka_coordinator_runtime import EventEnvelope
from misaka_event_source import EventSource as CloudEventSource


class CloudEventSourceAdapter:
    """Adapts the provider-neutral CloudEvent source to Coordinator events."""

    def __init__(self, source: CloudEventSource) -> None:
        self._source = source

    async def events(
        self,
        *,
        start_sequence: int = 1,
        topic: str | None = None,
    ) -> AsyncIterator[EventEnvelope]:
        async for event in self._source.events(start_sequence=start_sequence):
            if topic is not None and event.event_type != topic:
                continue
            if event.sequence < 1:
                raise RuntimeError(
                    f"event source returned unpublished event {event.event_id} without a sequence"
                )
            payload = dict(event.data)
            payload.setdefault("event_id", event.event_id)
            payload.setdefault("source", event.source)
            payload.setdefault("event_type", event.event_type)
            if event.subject is not None:
                payload.setdefault("subject", event.subject)
            yield EventEnvelope(
                sequence=event.sequence,
                event_id=event.event_id,
                topic=event.event_type,
                payload=payload,
                occurred_at=event.occurred_at,
            )

    async def close(self) -> None:
        await self._source.close()
