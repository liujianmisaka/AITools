from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from misaka_kernel_contracts import JsonObject


@dataclass(frozen=True, slots=True)
class CloudEvent:
    event_id: str
    source: str
    event_type: str
    data: JsonObject
    specversion: str = "1.0"
    subject: str | None = None
    datacontenttype: str = "application/json"
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    extensions: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in {
            "event_id": self.event_id,
            "source": self.source,
            "event_type": self.event_type,
            "specversion": self.specversion,
            "datacontenttype": self.datacontenttype,
        }.items():
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.specversion != "1.0":
            raise ValueError("only CloudEvents specversion 1.0 is supported")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")


class EventSource(Protocol):
    async def events(self, *, start_sequence: int = 1) -> AsyncIterator[CloudEvent]: ...

    async def close(self) -> None: ...
