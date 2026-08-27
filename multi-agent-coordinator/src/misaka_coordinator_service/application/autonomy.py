from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from misaka_coordinator_service.domain import (
    AutonomyApproval,
    AutonomyApprovalKind,
    AutonomyApprovalStatus,
    CoordinatorSession,
    PlanNode,
    PlanNodeStatus,
)
from misaka_coordinator_service.domain._serialization import ensure_text, ensure_text_tuple
from misaka_coordinator_service.domain.errors import CoordinatorDomainError

_WORKSPACE_WRITE_CAPABILITIES = frozenset({"workspace.write", "workspace_write"})
_DESTRUCTIVE_CAPABILITIES = frozenset({"operation.destructive", "destructive_operation"})
_LIVE_NODE_STATUSES = frozenset({PlanNodeStatus.DELEGATED, PlanNodeStatus.AWAITING_EVENT})


class CoordinatorPolicyError(RuntimeError):
    """Base error for deterministic Coordinator autonomy policy checks."""


class CoordinatorPolicyDeniedError(CoordinatorPolicyError):
    """Raised when a user explicitly denied the exact requested action."""


class CoordinatorPolicyApprovalRequired(CoordinatorPolicyError):
    """Carries the updated session that persists a newly requested approval."""

    def __init__(
        self,
        *,
        session: CoordinatorSession,
        approval: AutonomyApproval,
    ) -> None:
        super().__init__(approval.reason)
        self.session = session
        self.approval = approval


@dataclass(frozen=True, slots=True)
class AutonomyRequirement:
    kind: AutonomyApprovalKind
    action_key: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_key", ensure_text(self.action_key, "action_key"))
        object.__setattr__(self, "reason", ensure_text(self.reason, "reason"))


@dataclass(frozen=True, slots=True)
class AutonomyAuthorization:
    session: CoordinatorSession
    approvals: tuple[AutonomyApproval, ...]
    blocked_approval: AutonomyApproval | None = None

    @property
    def allowed(self) -> bool:
        return self.blocked_approval is None


@dataclass(frozen=True, slots=True)
class CoordinatorAutonomyPolicy:
    max_concurrent_delegations: int = 8
    max_total_delegations: int = 30
    max_delegation_depth: int = 3
    max_plan_revisions: int = 10
    max_retries_per_node: int = 2
    max_runtime_minutes: int = 120
    max_model_activations: int = 50
    allowed_provider_ids: tuple[str, ...] = ()
    allowed_workspace_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "max_concurrent_delegations",
            "max_total_delegations",
            "max_plan_revisions",
            "max_runtime_minutes",
            "max_model_activations",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or value < 1:
                raise CoordinatorPolicyError(f"{field_name} must be positive")
        for field_name in ("max_delegation_depth", "max_retries_per_node"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or value < 0:
                raise CoordinatorPolicyError(f"{field_name} must not be negative")
        try:
            provider_ids = ensure_text_tuple(self.allowed_provider_ids, "allowed_provider_ids")
            root_values = ensure_text_tuple(self.allowed_workspace_roots, "allowed_workspace_roots")
            roots = tuple(str(Path(root).expanduser().resolve()) for root in root_values)
        except (CoordinatorDomainError, OSError, ValueError) as error:
            raise CoordinatorPolicyError("autonomy scope configuration is invalid") from error
        object.__setattr__(self, "allowed_provider_ids", provider_ids)
        if len(roots) != len(set(roots)):
            raise CoordinatorPolicyError("allowed_workspace_roots must be unique")
        object.__setattr__(self, "allowed_workspace_roots", roots)

    def activation_requirements(
        self, session: CoordinatorSession, *, at: datetime
    ) -> tuple[AutonomyRequirement, ...]:
        next_activation = session.autonomy.model_activation_count + 1
        requirements: list[AutonomyRequirement] = []
        if next_activation > self.max_model_activations:
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.BUDGET_OVERRUN,
                    action_key=f"model_activation:{session.session_id}:{next_activation}",
                    reason=(
                        "Coordinator model activation budget would be exceeded "
                        f"({next_activation}>{self.max_model_activations})"
                    ),
                )
            )
        runtime_limit = session.created_at + timedelta(minutes=self.max_runtime_minutes)
        if at > runtime_limit:
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.BUDGET_OVERRUN,
                    action_key=(
                        f"runtime:{session.session_id}:{next_activation}:{self.max_runtime_minutes}"
                    ),
                    reason=(
                        "Coordinator runtime budget has expired "
                        f"({self.max_runtime_minutes} minutes)"
                    ),
                )
            )
        return tuple(requirements)

    def plan_revision_requirements(
        self, session: CoordinatorSession
    ) -> tuple[AutonomyRequirement, ...]:
        next_revision = session.autonomy.plan_revision_count + 1
        if next_revision <= self.max_plan_revisions:
            return ()
        return (
            AutonomyRequirement(
                kind=AutonomyApprovalKind.BUDGET_OVERRUN,
                action_key=f"plan_revision:{session.session_id}:{next_revision}",
                reason=(
                    "Coordinator plan revision budget would be exceeded "
                    f"({next_revision}>{self.max_plan_revisions})"
                ),
            ),
        )

    def delegation_requirements(
        self,
        *,
        session: CoordinatorSession,
        node: PlanNode,
        cwd: str,
    ) -> tuple[AutonomyRequirement, ...]:
        if session.plan is None:
            raise CoordinatorPolicyError("delegation policy requires a plan")
        if node.selection is None:
            raise CoordinatorPolicyError("delegation policy requires an agent selection")
        action_key = (
            f"delegate:{session.session_id}:{node.node_id}:attempt-{node.attempt}:"
            f"{node.selection.provider_id}:{Path(cwd).expanduser().resolve()}"
        )
        requirements: list[AutonomyRequirement] = []
        if (
            self.allowed_provider_ids
            and node.selection.provider_id not in self.allowed_provider_ids
        ):
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.PROVIDER_SCOPE_EXPANSION,
                    action_key=action_key,
                    reason=(
                        f"provider {node.selection.provider_id} is outside the configured "
                        "Coordinator provider scope"
                    ),
                )
            )
        if self.allowed_workspace_roots and not self._workspace_allowed(cwd):
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.NEW_WORKSPACE_ROOT,
                    action_key=action_key,
                    reason="delegation workspace is outside configured Coordinator roots",
                )
            )
        capabilities = frozenset(node.intent.required_capabilities)
        if capabilities & _WORKSPACE_WRITE_CAPABILITIES:
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.WORKSPACE_WRITE,
                    action_key=action_key,
                    reason="delegation requests workspace write capability",
                )
            )
        if capabilities & _DESTRUCTIVE_CAPABILITIES:
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.DESTRUCTIVE_OPERATION,
                    action_key=action_key,
                    reason="delegation requests destructive operation capability",
                )
            )
        next_total = session.autonomy.delegation_count + 1
        if next_total > self.max_total_delegations:
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.BUDGET_OVERRUN,
                    action_key=f"{action_key}:total-{next_total}",
                    reason=(
                        "Coordinator delegation budget would be exceeded "
                        f"({next_total}>{self.max_total_delegations})"
                    ),
                )
            )
        active_count = sum(
            candidate.status in _LIVE_NODE_STATUSES for candidate in session.plan.nodes
        )
        if active_count >= self.max_concurrent_delegations:
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.BUDGET_OVERRUN,
                    action_key=f"{action_key}:concurrent-{active_count + 1}",
                    reason=(
                        "Coordinator concurrent delegation budget would be exceeded "
                        f"({active_count + 1}>{self.max_concurrent_delegations})"
                    ),
                )
            )
        retry_count = node.attempt - 1
        if retry_count > self.max_retries_per_node:
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.BUDGET_OVERRUN,
                    action_key=f"{action_key}:retry-{retry_count}",
                    reason=(
                        f"node {node.node_id} retry budget would be exceeded "
                        f"({retry_count}>{self.max_retries_per_node})"
                    ),
                )
            )
        depth = self._delegation_depth(session, node)
        if depth > self.max_delegation_depth:
            requirements.append(
                AutonomyRequirement(
                    kind=AutonomyApprovalKind.BUDGET_OVERRUN,
                    action_key=f"{action_key}:depth-{depth}",
                    reason=(
                        f"node {node.node_id} delegation depth would be exceeded "
                        f"({depth}>{self.max_delegation_depth})"
                    ),
                )
            )
        return tuple(requirements)

    @staticmethod
    def reconciliation_requirements(
        *, session: CoordinatorSession, node: PlanNode, expected_revision: int
    ) -> tuple[AutonomyRequirement, ...]:
        return (
            AutonomyRequirement(
                kind=AutonomyApprovalKind.MANUAL_RECONCILIATION,
                action_key=(f"reconcile:{session.session_id}:{node.node_id}:{expected_revision}"),
                reason="manual reconciliation changes the authoritative V3 terminal fact",
            ),
        )

    @staticmethod
    def authorize(
        session: CoordinatorSession,
        requirements: tuple[AutonomyRequirement, ...],
        *,
        at: datetime,
    ) -> AutonomyAuthorization:
        current = session
        approved: list[AutonomyApproval] = []
        for requirement in requirements:
            approval = current.autonomy.approval_for(requirement.kind, requirement.action_key)
            if approval is None:
                autonomy = current.autonomy.request_approval(
                    kind=requirement.kind,
                    action_key=requirement.action_key,
                    reason=requirement.reason,
                    at=at,
                )
                current = current.update_autonomy(autonomy, at=at)
                approval = current.autonomy.approval_for(requirement.kind, requirement.action_key)
                if approval is None:
                    raise CoordinatorDomainError("requested approval was not persisted")
            if approval.status is AutonomyApprovalStatus.DENIED:
                raise CoordinatorPolicyDeniedError(f"approval {approval.approval_id} was denied")
            if approval.status is AutonomyApprovalStatus.PENDING:
                return AutonomyAuthorization(
                    session=current,
                    approvals=tuple(approved),
                    blocked_approval=approval,
                )
            if approval.status is AutonomyApprovalStatus.APPROVED:
                approved.append(approval)
        return AutonomyAuthorization(session=current, approvals=tuple(approved))

    @staticmethod
    def record_action(
        session: CoordinatorSession,
        approvals: tuple[AutonomyApproval, ...],
        *,
        at: datetime,
        model_activation: bool = False,
        delegation: bool = False,
        plan_revision: bool = False,
    ) -> CoordinatorSession:
        autonomy = session.autonomy.consume_approvals(approvals, at=at)
        if model_activation:
            autonomy = autonomy.record_model_activation()
        if delegation:
            autonomy = autonomy.record_delegation()
        if plan_revision:
            autonomy = autonomy.record_plan_revision()
        return session.update_autonomy(autonomy, at=at)

    def _workspace_allowed(self, cwd: str) -> bool:
        candidate = Path(cwd).expanduser().resolve()
        return any(
            candidate == Path(root) or candidate.is_relative_to(Path(root))
            for root in self.allowed_workspace_roots
        )

    @staticmethod
    def _delegation_depth(session: CoordinatorSession, node: PlanNode) -> int:
        if session.plan is None:
            raise CoordinatorPolicyError("delegation depth requires a plan")
        nodes = {candidate.intent.task_id: candidate for candidate in session.plan.nodes}
        depth = 0
        parent_task_id = node.intent.parent_task_id
        visited = {node.intent.task_id}
        while parent_task_id is not None:
            if parent_task_id in visited:
                raise CoordinatorPolicyError("delegation parent chain contains a cycle")
            visited.add(parent_task_id)
            parent = nodes.get(parent_task_id)
            if parent is None:
                raise CoordinatorPolicyError(f"delegation parent {parent_task_id} does not exist")
            depth += 1
            parent_task_id = parent.intent.parent_task_id
        return depth
