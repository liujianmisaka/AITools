from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

from misaka_kernel_contracts import EventMode, RuntimeEvent


class EventHandler(Protocol):
    async def __call__(self, event: RuntimeEvent) -> None: ...


class WaterfallHandler(Protocol):
    async def __call__(
        self,
        event: RuntimeEvent,
        next_handler: Callable[[], Awaitable[RuntimeEvent]],
    ) -> RuntimeEvent: ...


@dataclass(frozen=True, slots=True)
class DispatchFailure:
    event_name: str
    mode: EventMode
    handler_name: str
    error: Exception


class EventDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[tuple[str, EventMode], list[EventHandler | WaterfallHandler]] = {}

    def on(
        self,
        event_name: str,
        handler: EventHandler | WaterfallHandler,
        *,
        mode: EventMode = EventMode.EMIT,
    ) -> Callable[[], None]:
        if not event_name.strip():
            raise ValueError("event name must not be empty")
        key = (event_name, mode)
        handlers = self._handlers.setdefault(key, [])
        handlers.append(handler)
        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            if handler in handlers:
                handlers.remove(handler)
            if not handlers:
                self._handlers.pop(key, None)

        return unsubscribe

    async def emit(self, event: RuntimeEvent) -> tuple[DispatchFailure, ...]:
        failures: list[DispatchFailure] = []
        for handler in tuple(self._handlers.get((event.name, EventMode.EMIT), ())):
            try:
                await cast(EventHandler, handler)(event)
            except Exception as exc:
                failures.append(
                    DispatchFailure(event.name, EventMode.EMIT, _handler_name(handler), exc)
                )
        return tuple(failures)

    async def serial(self, event: RuntimeEvent) -> tuple[DispatchFailure, ...]:
        failures: list[DispatchFailure] = []
        for handler in tuple(self._handlers.get((event.name, EventMode.SERIAL), ())):
            try:
                await cast(EventHandler, handler)(event)
            except Exception as exc:
                failures.append(
                    DispatchFailure(event.name, EventMode.SERIAL, _handler_name(handler), exc)
                )
        return tuple(failures)

    async def parallel(self, event: RuntimeEvent) -> tuple[DispatchFailure, ...]:
        handlers = tuple(self._handlers.get((event.name, EventMode.PARALLEL), ()))
        results = await asyncio.gather(
            *(cast(EventHandler, handler)(event) for handler in handlers),
            return_exceptions=True,
        )
        failures: list[DispatchFailure] = []
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, Exception):
                failures.append(
                    DispatchFailure(event.name, EventMode.PARALLEL, _handler_name(handler), result)
                )
        return tuple(failures)

    async def waterfall(self, event: RuntimeEvent) -> RuntimeEvent:
        handlers = tuple(self._handlers.get((event.name, EventMode.WATERFALL), ()))

        async def invoke(index: int, current: RuntimeEvent) -> RuntimeEvent:
            if index == len(handlers):
                return current
            handler = cast(WaterfallHandler, handlers[index])

            async def next_handler() -> RuntimeEvent:
                return await invoke(index + 1, current)

            return await handler(current, next_handler)

        return await invoke(0, event)


def _handler_name(handler: object) -> str:
    return getattr(handler, "__qualname__", type(handler).__qualname__)

