from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from misaka_kernel_contracts import JsonObject, JsonValue


class CoordinatorStatus(StrEnum):
    STOPPED = "stopped"
    ACTIVE = "active"
    STOPPING = "stopping"


class ExecutionStatus(StrEnum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.RECONCILIATION_REQUIRED,
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    execution_id: str
    sequence: int
    status: ExecutionStatus
    payload: JsonObject = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.execution_id.strip():
            raise ValueError("execution_id must not be empty")
        if self.sequence < 1:
            raise ValueError("execution event sequence must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("execution event timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution_id: str
    status: ExecutionStatus
    activation_id: str | None = None
    output: JsonValue | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.execution_id.strip():
            raise ValueError("execution_id must not be empty")
        if self.activation_id is not None and not self.activation_id.strip():
            raise ValueError("activation_id must not be empty when provided")
        if self.status not in TERMINAL_EXECUTION_STATUSES:
            raise ValueError("execution result status must be terminal")


class ReconciliationState(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    state: ReconciliationState
    message: str | None = None
    output: JsonValue | None = None
    error_code: str | None = None
    error_message: str | None = None
    last_sequence: int = 0

    def __post_init__(self) -> None:
        if self.message is not None and not self.message.strip():
            raise ValueError("reconciliation message must not be empty")
        if self.error_code is not None and not self.error_code.strip():
            raise ValueError("reconciliation error code must not be empty")
        if self.last_sequence < 0:
            raise ValueError("reconciliation sequence must not be negative")


class ExecutionHandle(Protocol):
    @property
    def execution_id(self) -> str: ...

    @property
    def activation_id(self) -> str | None: ...

    def events(self, *, start_sequence: int = 1) -> AsyncIterator[ExecutionEvent]: ...

    async def wait(self) -> ExecutionResult: ...

    async def cancel(self, reason: str) -> None: ...

    async def reconcile(self) -> ReconciliationResult: ...


class ExecutionPlan(Protocol):
    @property
    def execution_id(self) -> str: ...

    @property
    def fingerprint(self) -> str: ...

    async def start(self, *, attempt: int = 1) -> ExecutionHandle: ...


ExecutionPlanFactory = Callable[[int], Awaitable[ExecutionPlan]]


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


class EventSource(Protocol):
    def events(
        self,
        *,
        start_sequence: int = 1,
        topic: str | None = None,
    ) -> AsyncIterator[EventEnvelope]: ...

    async def close(self) -> None: ...


EventRouteFactory = Callable[[EventEnvelope], Awaitable[ExecutionPlan | None]]


class EventDeliveryStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class EventDeliveryRecord:
    consumer_id: str
    event_id: str
    source_sequence: int
    status: EventDeliveryStatus
    attempts: int = 0
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.consumer_id.strip() or not self.event_id.strip():
            raise ValueError("event delivery identity must not be empty")
        if self.source_sequence < 1 or self.attempts < 0:
            raise ValueError("event delivery sequence and attempts must be valid")
        if self.error_message is not None and not self.error_message.strip():
            raise ValueError("event delivery error must be non-empty when provided")


class EventDeliveryStore(Protocol):
    async def get(self, consumer_id: str, event_id: str) -> EventDeliveryRecord | None: ...

    async def claim(
        self,
        consumer_id: str,
        event_id: str,
        source_sequence: int,
    ) -> EventDeliveryRecord: ...

    async def complete(
        self,
        record: EventDeliveryRecord,
        *,
        status: EventDeliveryStatus,
        error_message: str | None = None,
    ) -> EventDeliveryRecord: ...

    async def cursor(self, consumer_id: str) -> int: ...


class QueueJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


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
    result: ExecutionResult | None
    attempts: tuple[ExecutionResult, ...]
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class QueueJobSnapshot:
    job_id: str
    status: QueueJobStatus
    attempts: tuple[ExecutionResult, ...] = ()
    result: ExecutionResult | None = None
