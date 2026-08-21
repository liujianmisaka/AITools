from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

from misaka_kernel_contracts import (
    EventDeclaration,
    EventFailureIsolation,
    EventMode,
    RuntimeEvent,
    matches_event_schema,
)

from misaka_kernel.errors import EventDeclarationError


class EventHandler(Protocol):
    async def __call__(self, event: RuntimeEvent) -> None: ...


class WaterfallHandler(Protocol):
    async def __call__(
        self,
        event: RuntimeEvent,
        next_handler: Callable[[], Awaitable[RuntimeEvent]],
    ) -> RuntimeEvent: ...


class BailHandler(Protocol):
    async def __call__(self, event: RuntimeEvent) -> bool | None: ...


EventHandlerLike = EventHandler | WaterfallHandler | BailHandler


@dataclass(frozen=True, slots=True)
class DispatchFailure:
    event_name: str
    mode: EventMode
    handler_name: str
    error: Exception


@dataclass(frozen=True, slots=True)
class BailDispatchResult:
    decision: bool | None
    failures: tuple[DispatchFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class WaterfallDispatchResult:
    event: RuntimeEvent
    failures: tuple[DispatchFailure, ...] = ()


EventDispatchResult = tuple[DispatchFailure, ...] | BailDispatchResult | WaterfallDispatchResult


@dataclass(frozen=True, slots=True)
class _Subscription:
    handler: EventHandlerLike
    consumer_id: str
    scope_id: str


class EventDispatcher:
    """Typed, scoped event dispatcher with reversible declaration bindings."""

    def __init__(self, *, require_declarations: bool = True) -> None:
        self.require_declarations = require_declarations
        self._declarations: dict[str, EventDeclaration] = {}
        self._handlers: dict[tuple[str, EventMode], list[_Subscription]] = {}

    def declare(self, declaration: EventDeclaration) -> Callable[[], None]:
        existing = self._declarations.get(declaration.event_name)
        if existing is not None:
            raise EventDeclarationError(
                "event.declaration_conflict",
                f"event {declaration.event_name} is already declared",
            )
        self._declarations[declaration.event_name] = declaration
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            if self._declarations.get(declaration.event_name) == declaration:
                del self._declarations[declaration.event_name]

        return dispose

    def declarations(self) -> tuple[EventDeclaration, ...]:
        return tuple(self._declarations[name] for name in sorted(self._declarations))

    def on(
        self,
        event_name: str,
        handler: EventHandlerLike,
        *,
        mode: EventMode = EventMode.EMIT,
        consumer_id: str = "*",
        scope_id: str = "*",
    ) -> Callable[[], None]:
        if not event_name.strip():
            raise ValueError("event name must not be empty")
        if not consumer_id.strip():
            raise ValueError("event subscription consumer must not be empty")
        if not scope_id.strip():
            raise ValueError("event subscription scope must not be empty")
        declaration = self._declarations.get(event_name)
        if declaration is None and self.require_declarations:
            raise EventDeclarationError(
                "event.declaration_missing",
                f"event {event_name} must be declared before subscribing",
            )
        if declaration is not None and declaration.mode is not mode:
            raise EventDeclarationError(
                "event.mode_mismatch",
                f"event {event_name} is declared as {declaration.mode.value}",
            )
        if declaration is not None and (
            "*" not in declaration.consumer and consumer_id not in declaration.consumer
        ):
            raise EventDeclarationError(
                "event.consumer_forbidden",
                f"consumer {consumer_id} is not declared for event {event_name}",
            )
        key = (event_name, mode)
        subscription = _Subscription(handler, consumer_id, scope_id)
        handlers = self._handlers.setdefault(key, [])
        handlers.append(subscription)
        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            if subscription in handlers:
                handlers.remove(subscription)
            if not handlers:
                self._handlers.pop(key, None)

        return unsubscribe

    async def dispatch(self, event: RuntimeEvent) -> EventDispatchResult:
        declaration = self._validate_event(event)
        mode = declaration.mode if declaration is not None else EventMode.EMIT
        if mode is EventMode.EMIT:
            return await self.emit(event)
        if mode is EventMode.SERIAL:
            return await self.serial(event)
        if mode is EventMode.PARALLEL:
            return await self.parallel(event)
        if mode is EventMode.BAIL:
            return await self.bail(event)
        return await self.waterfall(event)

    async def emit(self, event: RuntimeEvent) -> tuple[DispatchFailure, ...]:
        declaration = self._validate_event(event, expected_mode=EventMode.EMIT)
        return await self._run_serial(event, EventMode.EMIT, declaration)

    async def serial(self, event: RuntimeEvent) -> tuple[DispatchFailure, ...]:
        declaration = self._validate_event(event, expected_mode=EventMode.SERIAL)
        return await self._run_serial(event, EventMode.SERIAL, declaration)

    async def parallel(self, event: RuntimeEvent) -> tuple[DispatchFailure, ...]:
        declaration = self._validate_event(event, expected_mode=EventMode.PARALLEL)
        subscriptions = self._subscriptions(event, EventMode.PARALLEL)
        results = await asyncio.gather(
            *(cast(EventHandler, item.handler)(event) for item in subscriptions),
            return_exceptions=True,
        )
        failures: list[DispatchFailure] = []
        for item, result in zip(subscriptions, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                if _propagates(declaration):
                    raise result
                failures.append(
                    DispatchFailure(
                        event.name,
                        EventMode.PARALLEL,
                        _handler_name(item.handler),
                        result,
                    )
                )
        return tuple(failures)

    async def bail(self, event: RuntimeEvent) -> BailDispatchResult:
        declaration = self._validate_event(event, expected_mode=EventMode.BAIL)
        failures: list[DispatchFailure] = []
        for item in self._subscriptions(event, EventMode.BAIL):
            try:
                decision = await cast(BailHandler, item.handler)(event)
                if decision is not None:
                    return BailDispatchResult(decision=decision, failures=tuple(failures))
            except Exception as exc:
                if _propagates(declaration):
                    raise
                failures.append(
                    DispatchFailure(event.name, EventMode.BAIL, _handler_name(item.handler), exc)
                )
        return BailDispatchResult(decision=None, failures=tuple(failures))

    async def waterfall(self, event: RuntimeEvent) -> WaterfallDispatchResult:
        declaration = self._validate_event(event, expected_mode=EventMode.WATERFALL)
        subscriptions = self._subscriptions(event, EventMode.WATERFALL)
        failures: list[DispatchFailure] = []

        async def invoke(index: int, current: RuntimeEvent) -> RuntimeEvent:
            if index == len(subscriptions):
                return current
            handler = cast(WaterfallHandler, subscriptions[index].handler)

            async def next_handler() -> RuntimeEvent:
                return await invoke(index + 1, current)

            try:
                return await handler(current, next_handler)
            except Exception as exc:
                if _propagates(declaration):
                    raise
                failures.append(
                    DispatchFailure(
                        event.name,
                        EventMode.WATERFALL,
                        _handler_name(subscriptions[index].handler),
                        exc,
                    )
                )
                return current

        result = await invoke(0, event)
        return WaterfallDispatchResult(event=result, failures=tuple(failures))

    def _validate_event(
        self,
        event: RuntimeEvent,
        *,
        expected_mode: EventMode | None = None,
    ) -> EventDeclaration | None:
        declaration = self._declarations.get(event.name)
        if declaration is None:
            if self.require_declarations:
                raise EventDeclarationError(
                    "event.declaration_missing",
                    f"event {event.name} must be declared before dispatch",
                )
            return None
        if expected_mode is not None and declaration.mode is not expected_mode:
            raise EventDeclarationError(
                "event.mode_mismatch",
                f"event {event.name} is declared as {declaration.mode.value}",
            )
        if event.version != declaration.version:
            raise EventDeclarationError(
                "event.version_mismatch",
                f"event {event.name} requires version {declaration.version}",
            )
        if declaration.scope != "*" and event.scope_id != declaration.scope:
            raise EventDeclarationError(
                "event.scope_mismatch",
                f"event {event.name} is outside declaration scope {declaration.scope}",
            )
        if declaration.producer != "*" and event.source != declaration.producer:
            raise EventDeclarationError(
                "event.producer_mismatch",
                f"event {event.name} must be produced by {declaration.producer}",
            )
        if not matches_event_schema(event.payload, declaration.payload_schema):
            raise EventDeclarationError(
                "event.payload_schema_invalid",
                f"event {event.name} payload does not satisfy its declaration schema",
            )
        return declaration

    def _subscriptions(self, event: RuntimeEvent, mode: EventMode) -> tuple[_Subscription, ...]:
        return tuple(
            item
            for item in self._handlers.get((event.name, mode), ())
            if item.scope_id in {"*", event.scope_id}
        )

    async def _run_serial(
        self,
        event: RuntimeEvent,
        mode: EventMode,
        declaration: EventDeclaration | None,
    ) -> tuple[DispatchFailure, ...]:
        failures: list[DispatchFailure] = []
        for item in self._subscriptions(event, mode):
            try:
                await cast(EventHandler, item.handler)(event)
            except Exception as exc:
                if _propagates(declaration):
                    raise
                failures.append(DispatchFailure(event.name, mode, _handler_name(item.handler), exc))
        return tuple(failures)


def _propagates(declaration: EventDeclaration | None) -> bool:
    return (
        declaration is not None and declaration.failure_isolation is EventFailureIsolation.PROPAGATE
    )


def _handler_name(handler: object) -> str:
    return getattr(handler, "__qualname__", type(handler).__qualname__)
