from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256

from misaka_coordinator_service.domain._serialization import (
    datetime_to_text,
    ensure_not_before,
    ensure_optional_text,
    ensure_text,
    ensure_utc,
    read_datetime,
    read_int,
    read_mapping_list,
    read_optional_text,
    read_text,
)
from misaka_coordinator_service.domain.errors import (
    CoordinatorDomainError,
    InvalidTransitionError,
)


class AutonomyApprovalKind(StrEnum):
    WORKSPACE_WRITE = "workspace_write"
    DESTRUCTIVE_OPERATION = "destructive_operation"
    NEW_WORKSPACE_ROOT = "new_workspace_root"
    UNREGISTERED_MCP_SERVER = "unregistered_mcp_server"
    PROVIDER_SCOPE_EXPANSION = "provider_scope_expansion"
    BUDGET_OVERRUN = "budget_overrun"
    MANUAL_RECONCILIATION = "manual_reconciliation"


class AutonomyApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class AutonomyApproval:
    approval_id: str
    kind: AutonomyApprovalKind
    action_key: str
    reason: str
    status: AutonomyApprovalStatus
    requested_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_reason: str | None = None
    consumed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "approval_id", ensure_text(self.approval_id, "approval_id"))
        object.__setattr__(self, "action_key", ensure_text(self.action_key, "action_key"))
        object.__setattr__(self, "reason", ensure_text(self.reason, "reason"))
        requested_at = ensure_utc(self.requested_at, "requested_at")
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(
            self,
            "resolved_by",
            ensure_optional_text(self.resolved_by, "resolved_by"),
        )
        object.__setattr__(
            self,
            "resolution_reason",
            ensure_optional_text(self.resolution_reason, "resolution_reason"),
        )
        if self.resolved_at is not None:
            object.__setattr__(
                self,
                "resolved_at",
                ensure_not_before(self.resolved_at, requested_at, "resolved_at"),
            )
        if self.consumed_at is not None:
            minimum = self.resolved_at or requested_at
            object.__setattr__(
                self,
                "consumed_at",
                ensure_not_before(self.consumed_at, minimum, "consumed_at"),
            )
        resolved_fields = (self.resolved_at, self.resolved_by, self.resolution_reason)
        if self.status is AutonomyApprovalStatus.PENDING:
            if any(value is not None for value in (*resolved_fields, self.consumed_at)):
                raise CoordinatorDomainError(
                    "pending autonomy approval cannot contain resolution fields"
                )
        elif any(value is None for value in resolved_fields):
            raise CoordinatorDomainError(
                "resolved autonomy approval requires resolver, time, and reason"
            )
        if self.status is AutonomyApprovalStatus.CONSUMED:
            if self.consumed_at is None:
                raise CoordinatorDomainError("consumed autonomy approval requires consumed_at")
        elif self.consumed_at is not None:
            raise CoordinatorDomainError(
                "only a consumed autonomy approval can contain consumed_at"
            )

    @classmethod
    def request(
        cls,
        *,
        kind: AutonomyApprovalKind,
        action_key: str,
        reason: str,
        at: datetime,
    ) -> AutonomyApproval:
        normalized_action_key = ensure_text(action_key, "action_key")
        digest = sha256(f"{kind.value}\0{normalized_action_key}".encode()).hexdigest()[:24]
        return cls(
            approval_id=f"approval-{digest}",
            kind=kind,
            action_key=normalized_action_key,
            reason=reason,
            status=AutonomyApprovalStatus.PENDING,
            requested_at=at,
        )

    def resolve(
        self,
        *,
        approved: bool,
        resolved_by: str,
        reason: str,
        at: datetime,
    ) -> AutonomyApproval:
        if self.status is not AutonomyApprovalStatus.PENDING:
            raise InvalidTransitionError(f"approval {self.approval_id} is already {self.status}")
        return replace(
            self,
            status=(AutonomyApprovalStatus.APPROVED if approved else AutonomyApprovalStatus.DENIED),
            resolved_at=at,
            resolved_by=resolved_by,
            resolution_reason=reason,
        )

    def consume(self, *, at: datetime) -> AutonomyApproval:
        if self.status is AutonomyApprovalStatus.CONSUMED:
            return self
        if self.status is not AutonomyApprovalStatus.APPROVED:
            raise InvalidTransitionError(
                f"approval {self.approval_id} cannot be consumed from {self.status}"
            )
        return replace(
            self,
            status=AutonomyApprovalStatus.CONSUMED,
            consumed_at=at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "kind": self.kind.value,
            "action_key": self.action_key,
            "reason": self.reason,
            "status": self.status.value,
            "requested_at": datetime_to_text(self.requested_at),
            "resolved_at": (
                None if self.resolved_at is None else datetime_to_text(self.resolved_at)
            ),
            "resolved_by": self.resolved_by,
            "resolution_reason": self.resolution_reason,
            "consumed_at": (
                None if self.consumed_at is None else datetime_to_text(self.consumed_at)
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> AutonomyApproval:
        resolved_at = (
            None if data.get("resolved_at") is None else read_datetime(data, "resolved_at")
        )
        consumed_at = (
            None if data.get("consumed_at") is None else read_datetime(data, "consumed_at")
        )
        return cls(
            approval_id=read_text(data, "approval_id"),
            kind=AutonomyApprovalKind(read_text(data, "kind")),
            action_key=read_text(data, "action_key"),
            reason=read_text(data, "reason"),
            status=AutonomyApprovalStatus(read_text(data, "status")),
            requested_at=read_datetime(data, "requested_at"),
            resolved_at=resolved_at,
            resolved_by=read_optional_text(data, "resolved_by"),
            resolution_reason=read_optional_text(data, "resolution_reason"),
            consumed_at=consumed_at,
        )


@dataclass(frozen=True, slots=True)
class CoordinatorAutonomyState:
    model_activation_count: int = 0
    delegation_count: int = 0
    plan_revision_count: int = 0
    approvals: tuple[AutonomyApproval, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "model_activation_count",
            "delegation_count",
            "plan_revision_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or value < 0:
                raise CoordinatorDomainError(f"{field_name} must not be negative")
        approval_ids = tuple(approval.approval_id for approval in self.approvals)
        if len(approval_ids) != len(set(approval_ids)):
            raise CoordinatorDomainError("autonomy approval_id values must be unique")
        action_scopes = tuple((approval.kind, approval.action_key) for approval in self.approvals)
        if len(action_scopes) != len(set(action_scopes)):
            raise CoordinatorDomainError(
                "autonomy approval kind and action_key values must be unique"
            )

    def approval_for(self, kind: AutonomyApprovalKind, action_key: str) -> AutonomyApproval | None:
        normalized_action_key = ensure_text(action_key, "action_key")
        return next(
            (
                approval
                for approval in self.approvals
                if approval.kind is kind and approval.action_key == normalized_action_key
            ),
            None,
        )

    def request_approval(
        self,
        *,
        kind: AutonomyApprovalKind,
        action_key: str,
        reason: str,
        at: datetime,
    ) -> CoordinatorAutonomyState:
        if self.approval_for(kind, action_key) is not None:
            return self
        return replace(
            self,
            approvals=(
                *self.approvals,
                AutonomyApproval.request(
                    kind=kind,
                    action_key=action_key,
                    reason=reason,
                    at=at,
                ),
            ),
        )

    def resolve_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        resolved_by: str,
        reason: str,
        at: datetime,
    ) -> CoordinatorAutonomyState:
        normalized_approval_id = ensure_text(approval_id, "approval_id")
        found = False
        approvals: list[AutonomyApproval] = []
        for approval in self.approvals:
            if approval.approval_id != normalized_approval_id:
                approvals.append(approval)
                continue
            found = True
            approvals.append(
                approval.resolve(
                    approved=approved,
                    resolved_by=resolved_by,
                    reason=reason,
                    at=at,
                )
            )
        if not found:
            raise CoordinatorDomainError(f"unknown autonomy approval {normalized_approval_id}")
        return replace(self, approvals=tuple(approvals))

    def consume_approvals(
        self, approvals: tuple[AutonomyApproval, ...], *, at: datetime
    ) -> CoordinatorAutonomyState:
        approval_ids = {approval.approval_id for approval in approvals}
        if not approval_ids:
            return self
        return replace(
            self,
            approvals=tuple(
                approval.consume(at=at) if approval.approval_id in approval_ids else approval
                for approval in self.approvals
            ),
        )

    def record_model_activation(self) -> CoordinatorAutonomyState:
        return replace(self, model_activation_count=self.model_activation_count + 1)

    def record_delegation(self) -> CoordinatorAutonomyState:
        return replace(self, delegation_count=self.delegation_count + 1)

    def record_plan_revision(self) -> CoordinatorAutonomyState:
        return replace(self, plan_revision_count=self.plan_revision_count + 1)

    def to_dict(self) -> dict[str, object]:
        return {
            "model_activation_count": self.model_activation_count,
            "delegation_count": self.delegation_count,
            "plan_revision_count": self.plan_revision_count,
            "approvals": [approval.to_dict() for approval in self.approvals],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CoordinatorAutonomyState:
        return cls(
            model_activation_count=read_int(data, "model_activation_count"),
            delegation_count=read_int(data, "delegation_count"),
            plan_revision_count=read_int(data, "plan_revision_count"),
            approvals=tuple(
                AutonomyApproval.from_dict(value) for value in read_mapping_list(data, "approvals")
            ),
        )
