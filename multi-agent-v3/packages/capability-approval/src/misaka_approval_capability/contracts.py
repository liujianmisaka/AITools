from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    DECIDED = "decided"


class ApprovalDecisionValue(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: str
    instance_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.approval_id.strip() or not self.instance_id.strip():
            raise ValueError("approval_id and instance_id must not be empty")
        if self.created_at.tzinfo is None:
            raise ValueError("approval created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    value: ApprovalDecisionValue
    reason: str = ""
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.decided_at.tzinfo is None:
            raise ValueError("approval decided_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    request: ApprovalRequest
    status: ApprovalStatus
    decision: ApprovalDecision | None = None

    def __post_init__(self) -> None:
        if self.status is ApprovalStatus.PENDING and self.decision is not None:
            raise ValueError("pending approval cannot contain a decision")
        if self.status is ApprovalStatus.DECIDED and self.decision is None:
            raise ValueError("decided approval must contain a decision")


class ApprovalStore(Protocol):
    async def ensure(self, request: ApprovalRequest) -> ApprovalRecord: ...

    async def get(self, approval_id: str) -> ApprovalRecord: ...

    async def list(self) -> tuple[ApprovalRecord, ...]: ...

    async def decide(self, approval_id: str, decision: ApprovalDecision) -> ApprovalRecord: ...
