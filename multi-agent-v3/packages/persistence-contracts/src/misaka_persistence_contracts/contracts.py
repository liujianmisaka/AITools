from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import Protocol, TypeVar

from misaka_kernel_contracts import JsonObject

StateT_co = TypeVar("StateT_co", covariant=True)
StateT = TypeVar("StateT")
CURRENT_DURABLE_FORMAT_VERSION = 1


class DurableJobStatus(StrEnum):
    QUEUED = "queued"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class DurableEvent:
    stream_id: str
    sequence: int
    event_id: str
    event_type: str
    payload: JsonObject
    schema_version: int = CURRENT_DURABLE_FORMAT_VERSION
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for name, value in {
            "stream_id": self.stream_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
        }.items():
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.sequence < 1:
            raise ValueError("sequence must be positive")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DurableJob:
    job_id: str
    idempotency_key: str
    request: JsonObject
    status: DurableJobStatus
    version: int = 1
    result: JsonObject | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.job_id.strip() or not self.idempotency_key.strip():
            raise ValueError("job_id and idempotency_key must not be empty")
        if self.version < 1:
            raise ValueError("version must be positive")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("job timestamps must be timezone-aware")


class DurableEventStore(Protocol):
    async def append(
        self,
        stream_id: str,
        event_id: str,
        event_type: str,
        payload: JsonObject,
        *,
        occurred_at: datetime | None = None,
    ) -> DurableEvent: ...

    async def read(
        self, stream_id: str, *, start_sequence: int = 1
    ) -> tuple[DurableEvent, ...]: ...


class DurableProjection(Protocol[StateT_co]):
    async def reset(self) -> None: ...

    async def apply(self, event: DurableEvent) -> None: ...

    def snapshot(self) -> StateT_co: ...


@dataclass(frozen=True, slots=True)
class ProjectionCheckpoint:
    stream_id: str
    source_sequence: int
    applied_sequence: int
    projection_version: int = 1

    def __post_init__(self) -> None:
        if not self.stream_id.strip():
            raise ValueError("stream_id must not be empty")
        if self.source_sequence < 0 or self.applied_sequence < 0:
            raise ValueError("projection sequences must not be negative")
        if self.applied_sequence > self.source_sequence:
            raise ValueError("applied_sequence must not exceed source_sequence")
        if self.projection_version < 1:
            raise ValueError("projection_version must be positive")


@dataclass(frozen=True, slots=True)
class ProjectionReplay[StateT]:
    state: StateT
    checkpoint: ProjectionCheckpoint


async def replay_events[StateT](
    events: Iterable[DurableEvent],
    projection: DurableProjection[StateT],
    *,
    reset: bool = True,
) -> StateT:
    if reset:
        await projection.reset()
    for event in events:
        await projection.apply(event)
    return projection.snapshot()


async def replay_events_with_checkpoint[StateT](
    events: Iterable[DurableEvent],
    projection: DurableProjection[StateT],
    *,
    stream_id: str | None = None,
    source_sequence: int | None = None,
    reset: bool = True,
    projection_version: int = 1,
) -> ProjectionReplay[StateT]:
    event_list = tuple(events)
    if not event_list and stream_id is None:
        raise ValueError("stream_id is required when replaying an empty event set")
    resolved_stream_id = stream_id or event_list[0].stream_id
    if not resolved_stream_id.strip():
        raise ValueError("stream_id must not be empty")
    if any(event.stream_id != resolved_stream_id for event in event_list):
        raise ValueError("projection replay events must belong to one stream")
    if any(current.sequence != previous.sequence + 1 for previous, current in pairwise(event_list)):
        raise ValueError("projection replay events must have contiguous sequences")
    if reset:
        await projection.reset()
    if source_sequence is None:
        source_sequence = event_list[-1].sequence if event_list else 0
    applied_sequence = (
        event_list[-1].sequence if event_list else (source_sequence if not reset else 0)
    )
    checkpoint = ProjectionCheckpoint(
        stream_id=resolved_stream_id,
        source_sequence=source_sequence,
        applied_sequence=applied_sequence,
        projection_version=projection_version,
    )
    for event in event_list:
        await projection.apply(event)
    return ProjectionReplay(projection.snapshot(), checkpoint)


class DurableJobRegistry(Protocol):
    async def register(
        self, job_id: str, idempotency_key: str, request: JsonObject
    ) -> tuple[DurableJob, bool]: ...

    async def get(self, job_id: str) -> DurableJob: ...

    async def list(self) -> tuple[DurableJob, ...]: ...

    async def transition(
        self,
        job_id: str,
        status: DurableJobStatus,
        *,
        expected_version: int | None = None,
        result: JsonObject | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> DurableJob: ...
