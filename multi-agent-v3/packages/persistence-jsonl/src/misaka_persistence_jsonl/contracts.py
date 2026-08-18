from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from misaka_kernel_contracts import JsonObject


class DurableJobStatus(StrEnum):
    QUEUED = "queued"
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
