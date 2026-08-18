from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from misaka_invocation_contracts import InvocationRequest, InvocationResult
from misaka_kernel_contracts import JsonObject


class CoordinatorStatus(StrEnum):
    STOPPED = "stopped"
    ACTIVE = "active"
    STOPPING = "stopping"


class QueueJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    sequence: int
    event_id: str
    topic: str
    payload: JsonObject
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("event sequence must be positive")
        if not self.event_id.strip() or not self.topic.strip():
            raise ValueError("event id and topic must not be empty")
        if self.occurred_at.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CoordinatorEvent:
    job_id: str
    sequence: int
    status: QueueJobStatus
    payload: JsonObject = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job id must not be empty")
        if self.sequence < 1:
            raise ValueError("coordinator event sequence must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class QueueJobResult:
    job_id: str
    status: QueueJobStatus
    result: InvocationResult | None
    attempts: tuple[InvocationResult, ...]
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class QueueJobSnapshot:
    job_id: str
    status: QueueJobStatus
    attempts: tuple[InvocationResult, ...] = ()
    result: InvocationResult | None = None


class EventSource(Protocol):
    def events(
        self,
        *,
        start_sequence: int = 1,
        topic: str | None = None,
    ) -> AsyncIterator[EventEnvelope]: ...


EventRouteFactory = Callable[[EventEnvelope], Awaitable[InvocationRequest | None]]
