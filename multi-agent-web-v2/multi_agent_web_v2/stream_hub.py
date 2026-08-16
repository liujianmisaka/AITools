from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StreamModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda value: "".join(
            word if index == 0 else word.capitalize() for index, word in enumerate(value.split("_"))
        ),
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


class TokenEvent(StreamModel):
    execution_id: str = Field(min_length=1, max_length=512)
    sequence: int = Field(ge=1)
    kind: Literal["text_delta", "status", "tool", "terminal"]
    text: str | None = Field(default=None, max_length=65_536)
    data: dict[str, object] = Field(default_factory=dict)


class TokenBatch(StreamModel):
    events: tuple[TokenEvent, ...] = Field(min_length=1, max_length=256)


@dataclass(frozen=True, slots=True)
class StreamHubStatus:
    subscribers: int
    tracked_executions: int
    dropped_events: int


class StreamHub:
    def __init__(self, *, queue_size: int = 512, execution_cache_size: int = 4096) -> None:
        if queue_size < 1 or execution_cache_size < 1:
            raise ValueError("stream hub bounds must be positive")
        self._queue_size = queue_size
        self._execution_cache_size = execution_cache_size
        self._subscribers: set[asyncio.Queue[TokenEvent]] = set()
        self._last_sequences: OrderedDict[str, int] = OrderedDict()
        self._dropped_events = 0
        self._lock = asyncio.Lock()

    async def publish(self, batch: TokenBatch) -> int:
        accepted = 0
        async with self._lock:
            for event in batch.events:
                previous = self._last_sequences.get(event.execution_id, 0)
                if event.sequence <= previous:
                    continue
                self._last_sequences[event.execution_id] = event.sequence
                self._last_sequences.move_to_end(event.execution_id)
                while len(self._last_sequences) > self._execution_cache_size:
                    self._last_sequences.popitem(last=False)
                accepted += 1
                for queue in self._subscribers:
                    if queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        else:
                            self._dropped_events += 1
                    queue.put_nowait(event)
        return accepted

    @asynccontextmanager
    async def subscribe(self) -> AsyncGenerator[asyncio.Queue[TokenEvent]]:
        queue: asyncio.Queue[TokenEvent] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def status(self) -> StreamHubStatus:
        async with self._lock:
            return StreamHubStatus(
                subscribers=len(self._subscribers),
                tracked_executions=len(self._last_sequences),
                dropped_events=self._dropped_events,
            )
