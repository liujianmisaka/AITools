from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from multi_agent.domain.errors import EventSourceNotFoundError
from multi_agent.domain.models import TriggerBindingDefinition, TriggerEventInput


@dataclass(frozen=True, slots=True)
class SourcePollResult:
    events: tuple[TriggerEventInput, ...]
    cursor: dict[str, Any]


class EventSourceDriver(ABC):
    source_type: str
    delivery_mode: str

    def validate_binding(self, binding: TriggerBindingDefinition) -> None:
        del binding

    def describe(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "delivery_mode": self.delivery_mode,
            "supports_polling": self.delivery_mode in {"poll", "hybrid"},
            "supports_push": self.delivery_mode in {"push", "hybrid"},
        }

    async def poll(
        self,
        binding: Mapping[str, Any],
        cursor: Mapping[str, Any] | None,
    ) -> SourcePollResult:
        del binding, cursor
        raise RuntimeError(f"event source {self.source_type!r} is not pollable")


class ManualEventSource(EventSourceDriver):
    source_type = "manual"
    delivery_mode = "push"


class FakeEventSource(EventSourceDriver):
    """Deterministic poll source used by tests; it performs no external I/O."""

    source_type = "fake"
    delivery_mode = "hybrid"

    def __init__(self) -> None:
        self._events: dict[str, list[TriggerEventInput]] = defaultdict(list)

    def emit(self, event: TriggerEventInput) -> None:
        if event.source_type != self.source_type:
            raise ValueError("fake source can only emit source_type='fake'")
        self._events[event.source_key or ""].append(event)

    async def poll(
        self,
        binding: Mapping[str, Any],
        cursor: Mapping[str, Any] | None,
    ) -> SourcePollResult:
        source_key = str(binding.get("source_key") or "")
        offset = int((cursor or {}).get("offset", 0))
        events = self._events[source_key][offset:]
        return SourcePollResult(
            events=tuple(events),
            cursor={"offset": offset + len(events)},
        )


class EventSourceRegistry:
    def __init__(self, sources: Iterable[EventSourceDriver] = ()) -> None:
        self._sources: dict[str, EventSourceDriver] = {}
        for source in sources:
            self.register(source)

    def register(self, source: EventSourceDriver) -> None:
        if not source.source_type:
            raise ValueError("event source type cannot be empty")
        if source.delivery_mode not in {"push", "poll", "hybrid"}:
            raise ValueError(
                f"invalid delivery mode for {source.source_type!r}: "
                f"{source.delivery_mode!r}"
            )
        if source.source_type in self._sources:
            raise ValueError(
                f"event source already registered: {source.source_type}"
            )
        self._sources[source.source_type] = source

    def get(self, source_type: str) -> EventSourceDriver:
        try:
            return self._sources[source_type]
        except KeyError as exc:
            raise EventSourceNotFoundError(
                f"event source not found: {source_type}"
            ) from exc

    def describe(self) -> list[dict[str, object]]:
        return [
            source.describe()
            for source in sorted(
                self._sources.values(), key=lambda item: item.source_type
            )
        ]
