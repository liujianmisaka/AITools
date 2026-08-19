from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from misaka_kernel_contracts import JsonObject

from misaka_persistence_contracts.contracts import (
    DurableEvent,
    DurableProjection,
    ProjectionReplay,
)

StateT = TypeVar("StateT")


@dataclass(frozen=True, slots=True)
class SessionHeader:
    session_id: str
    owner_id: str
    scope_id: str
    composition_id: str
    parent_session_id: str | None = None
    metadata: JsonObject = field(default_factory=dict)
    format_version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for name, value in {
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "scope_id": self.scope_id,
            "composition_id": self.composition_id,
        }.items():
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.parent_session_id is not None and not self.parent_session_id.strip():
            raise ValueError("parent_session_id must not be empty when provided")
        if self.format_version < 1:
            raise ValueError("format_version must be positive")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SessionInspection:
    header: SessionHeader
    last_sequence: int = 0
    fact_count: int = 0

    def __post_init__(self) -> None:
        if self.last_sequence < 0 or self.fact_count < 0:
            raise ValueError("session inspection counts must not be negative")
        if self.fact_count > self.last_sequence:
            raise ValueError("fact_count must not exceed last_sequence")


class SessionLog(Protocol):
    async def create(self, header: SessionHeader) -> SessionHeader: ...

    async def get(self, session_id: str) -> SessionHeader: ...

    async def list(self) -> tuple[SessionHeader, ...]: ...

    async def append(
        self,
        session_id: str,
        fact_id: str,
        fact_type: str,
        payload: JsonObject,
        *,
        schema_version: int = 1,
        occurred_at: datetime | None = None,
    ) -> DurableEvent: ...

    async def read(
        self, session_id: str, *, start_sequence: int = 1
    ) -> tuple[DurableEvent, ...]: ...

    async def inspect(self, session_id: str) -> SessionInspection: ...

    async def replay[StateT](
        self,
        session_id: str,
        projection: DurableProjection[StateT],
        *,
        start_sequence: int = 1,
        reset: bool = True,
        projection_version: int = 1,
    ) -> ProjectionReplay[StateT]: ...

    def events(
        self, session_id: str, *, start_sequence: int = 1
    ) -> AsyncIterator[DurableEvent]: ...
