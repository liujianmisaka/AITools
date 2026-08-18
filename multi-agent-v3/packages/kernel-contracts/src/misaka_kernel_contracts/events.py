from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from misaka_kernel_contracts.errors import ContractError

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class EventMode(StrEnum):
    EMIT = "emit"
    WATERFALL = "waterfall"
    SERIAL = "serial"
    PARALLEL = "parallel"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    name: str
    payload: JsonObject = field(default_factory=dict)
    source: str = "kernel"
    correlation_id: str | None = None
    causation_id: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ContractError("event.name_empty", "event name must not be empty")
        if not self.source.strip():
            raise ContractError("event.source_empty", "event source must not be empty")
        if self.occurred_at.tzinfo is None:
            raise ContractError("event.timestamp_naive", "event timestamp must be timezone-aware")
