from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from misaka_coordinator_service.domain._serialization import (
    datetime_to_text,
    ensure_not_before,
    ensure_optional_text,
    ensure_text,
    ensure_text_tuple,
    ensure_utc,
    read_datetime,
    read_int,
    read_mapping,
    read_mapping_list,
    read_optional_mapping,
    read_optional_text,
    read_text,
    read_text_tuple,
)
from misaka_coordinator_service.domain.errors import (
    CoordinatorDomainError,
    InvalidTransitionError,
)


class GoalStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanNodeStatus(StrEnum):
    PROPOSED = "proposed"
    READY = "ready"
    DELEGATED = "delegated"
    AWAITING_EVENT = "awaiting_event"
    REVIEW_REQUIRED = "review_required"
    ACCEPTED = "accepted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewDecisionKind(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    RETRY = "retry"
    ESCALATE = "escalate"


class CoordinatorEventType(StrEnum):
    USER_MESSAGE = "user_message"
    DELEGATION_CHANGED = "delegation_changed"
    OUTPUT_AVAILABLE = "output_available"
    TIMER_FIRED = "timer_fired"
    APPROVAL_RESOLVED = "approval_resolved"
    SERVICE_RECOVERED = "service_recovered"


@dataclass(frozen=True, slots=True)
class Goal:
    goal_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    status: GoalStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal_id", ensure_text(self.goal_id, "goal_id"))
        object.__setattr__(self, "objective", ensure_text(self.objective, "objective"))
        object.__setattr__(
            self,
            "acceptance_criteria",
            ensure_text_tuple(self.acceptance_criteria, "acceptance_criteria"),
        )
        object.__setattr__(
            self,
            "constraints",
            ensure_text_tuple(self.constraints, "constraints"),
        )
        created_at = ensure_utc(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(
            self,
            "updated_at",
            ensure_not_before(self.updated_at, created_at, "updated_at"),
        )

    def transition(self, status: GoalStatus, *, at: datetime) -> Goal:
        if self.status is not GoalStatus.ACTIVE:
            raise InvalidTransitionError(f"goal {self.goal_id} is already {self.status}")
        if status is GoalStatus.ACTIVE:
            raise InvalidTransitionError("goal transition must change status")
        return replace(self, status=status, updated_at=self._next_time(at))

    def _next_time(self, at: datetime) -> datetime:
        return ensure_not_before(at, self.updated_at, "at")

    def to_dict(self) -> dict[str, object]:
        return {
            "goal_id": self.goal_id,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "constraints": list(self.constraints),
            "status": self.status.value,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Goal:
        return cls(
            goal_id=read_text(data, "goal_id"),
            objective=read_text(data, "objective"),
            acceptance_criteria=read_text_tuple(data, "acceptance_criteria"),
            constraints=read_text_tuple(data, "constraints"),
            status=GoalStatus(read_text(data, "status")),
            created_at=read_datetime(data, "created_at"),
            updated_at=read_datetime(data, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class TaskIntent:
    task_id: str
    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    parent_task_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", ensure_text(self.task_id, "task_id"))
        object.__setattr__(self, "objective", ensure_text(self.objective, "objective"))
        object.__setattr__(
            self,
            "acceptance_criteria",
            ensure_text_tuple(self.acceptance_criteria, "acceptance_criteria"),
        )
        object.__setattr__(
            self,
            "required_capabilities",
            ensure_text_tuple(self.required_capabilities, "required_capabilities"),
        )
        object.__setattr__(
            self,
            "constraints",
            ensure_text_tuple(self.constraints, "constraints"),
        )
        object.__setattr__(
            self,
            "parent_task_id",
            ensure_optional_text(self.parent_task_id, "parent_task_id"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "required_capabilities": list(self.required_capabilities),
            "constraints": list(self.constraints),
            "parent_task_id": self.parent_task_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> TaskIntent:
        return cls(
            task_id=read_text(data, "task_id"),
            objective=read_text(data, "objective"),
            acceptance_criteria=read_text_tuple(data, "acceptance_criteria"),
            required_capabilities=read_text_tuple(data, "required_capabilities"),
            constraints=read_text_tuple(data, "constraints"),
            parent_task_id=read_optional_text(data, "parent_task_id"),
        )


@dataclass(frozen=True, slots=True)
class AgentSelection:
    provider_id: str
    model_id: str
    effort: str | None
    rationale: str
    capability_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", ensure_text(self.provider_id, "provider_id"))
        object.__setattr__(self, "model_id", ensure_text(self.model_id, "model_id"))
        object.__setattr__(self, "effort", ensure_optional_text(self.effort, "effort"))
        object.__setattr__(self, "rationale", ensure_text(self.rationale, "rationale"))
        object.__setattr__(
            self,
            "capability_ids",
            ensure_text_tuple(self.capability_ids, "capability_ids"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "effort": self.effort,
            "rationale": self.rationale,
            "capability_ids": list(self.capability_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> AgentSelection:
        return cls(
            provider_id=read_text(data, "provider_id"),
            model_id=read_text(data, "model_id"),
            effort=read_optional_text(data, "effort"),
            rationale=read_text(data, "rationale"),
            capability_ids=read_text_tuple(data, "capability_ids"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionReference:
    delegation_id: str
    activation_id: str | None = None
    invocation_id: str | None = None
    worker_session_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "delegation_id",
            ensure_text(self.delegation_id, "delegation_id"),
        )
        for field_name in ("activation_id", "invocation_id", "worker_session_id"):
            object.__setattr__(
                self,
                field_name,
                ensure_optional_text(getattr(self, field_name), field_name),
            )
        if self.invocation_id is not None and self.activation_id is None:
            raise CoordinatorDomainError("invocation_id requires activation_id")
        if self.worker_session_id is not None and self.invocation_id is None:
            raise CoordinatorDomainError("worker_session_id requires invocation_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "delegation_id": self.delegation_id,
            "activation_id": self.activation_id,
            "invocation_id": self.invocation_id,
            "worker_session_id": self.worker_session_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ExecutionReference:
        return cls(
            delegation_id=read_text(data, "delegation_id"),
            activation_id=read_optional_text(data, "activation_id"),
            invocation_id=read_optional_text(data, "invocation_id"),
            worker_session_id=read_optional_text(data, "worker_session_id"),
        )


@dataclass(frozen=True, slots=True)
class PlanNode:
    node_id: str
    intent: TaskIntent
    status: PlanNodeStatus
    selection: AgentSelection | None
    execution: ExecutionReference | None
    attempt: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", ensure_text(self.node_id, "node_id"))
        if self.attempt < 1:
            raise CoordinatorDomainError("attempt must be at least 1")
        created_at = ensure_utc(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(
            self,
            "updated_at",
            ensure_not_before(self.updated_at, created_at, "updated_at"),
        )
        if self.status is PlanNodeStatus.PROPOSED and (
            self.selection is not None or self.execution is not None
        ):
            raise CoordinatorDomainError("proposed node cannot have selection or execution")
        if self.status is PlanNodeStatus.READY and (
            self.selection is None or self.execution is not None
        ):
            raise CoordinatorDomainError("ready node requires selection and no execution")
        if self.status in {
            PlanNodeStatus.DELEGATED,
            PlanNodeStatus.AWAITING_EVENT,
            PlanNodeStatus.REVIEW_REQUIRED,
            PlanNodeStatus.ACCEPTED,
        } and (self.selection is None or self.execution is None):
            raise CoordinatorDomainError(f"{self.status} node requires selection and execution")
        if self.execution is not None and self.selection is None:
            raise CoordinatorDomainError("execution requires an agent selection")

    @classmethod
    def propose(cls, *, node_id: str, intent: TaskIntent, at: datetime) -> PlanNode:
        return cls(
            node_id=node_id,
            intent=intent,
            status=PlanNodeStatus.PROPOSED,
            selection=None,
            execution=None,
            attempt=1,
            created_at=at,
            updated_at=at,
        )

    def select(self, selection: AgentSelection, *, at: datetime) -> PlanNode:
        self._require_status(PlanNodeStatus.PROPOSED)
        return replace(
            self,
            status=PlanNodeStatus.READY,
            selection=selection,
            updated_at=self._next_time(at),
        )

    def bind_execution(self, execution: ExecutionReference, *, at: datetime) -> PlanNode:
        self._require_status(PlanNodeStatus.READY)
        return replace(
            self,
            status=PlanNodeStatus.DELEGATED,
            execution=execution,
            updated_at=self._next_time(at),
        )

    def await_event(self, *, at: datetime) -> PlanNode:
        self._require_status(PlanNodeStatus.DELEGATED)
        return replace(
            self,
            status=PlanNodeStatus.AWAITING_EVENT,
            updated_at=self._next_time(at),
        )

    def request_review(self, *, at: datetime) -> PlanNode:
        if self.status not in {PlanNodeStatus.DELEGATED, PlanNodeStatus.AWAITING_EVENT}:
            raise InvalidTransitionError(
                f"node {self.node_id} cannot enter review from {self.status}"
            )
        return replace(
            self,
            status=PlanNodeStatus.REVIEW_REQUIRED,
            updated_at=self._next_time(at),
        )

    def accept(self, *, at: datetime) -> PlanNode:
        self._require_status(PlanNodeStatus.REVIEW_REQUIRED)
        return replace(
            self,
            status=PlanNodeStatus.ACCEPTED,
            updated_at=self._next_time(at),
        )

    def retry(self, *, at: datetime, selection: AgentSelection | None = None) -> PlanNode:
        if self.status not in {PlanNodeStatus.REVIEW_REQUIRED, PlanNodeStatus.FAILED}:
            raise InvalidTransitionError(f"node {self.node_id} cannot retry from {self.status}")
        next_selection = selection if selection is not None else self.selection
        if next_selection is None:
            raise CoordinatorDomainError("retry requires an agent selection")
        return replace(
            self,
            status=PlanNodeStatus.READY,
            selection=next_selection,
            execution=None,
            attempt=self.attempt + 1,
            updated_at=self._next_time(at),
        )

    def fail(self, *, at: datetime) -> PlanNode:
        if self.status in {
            PlanNodeStatus.PROPOSED,
            PlanNodeStatus.ACCEPTED,
            PlanNodeStatus.FAILED,
            PlanNodeStatus.CANCELLED,
        }:
            raise InvalidTransitionError(f"node {self.node_id} cannot fail from {self.status}")
        return replace(
            self,
            status=PlanNodeStatus.FAILED,
            updated_at=self._next_time(at),
        )

    def cancel(self, *, at: datetime) -> PlanNode:
        if self.status in {
            PlanNodeStatus.ACCEPTED,
            PlanNodeStatus.FAILED,
            PlanNodeStatus.CANCELLED,
        }:
            raise InvalidTransitionError(f"node {self.node_id} cannot cancel from {self.status}")
        return replace(
            self,
            status=PlanNodeStatus.CANCELLED,
            updated_at=self._next_time(at),
        )

    def _require_status(self, status: PlanNodeStatus) -> None:
        if self.status is not status:
            raise InvalidTransitionError(
                f"node {self.node_id} must be {status}, current status is {self.status}"
            )

    def _next_time(self, at: datetime) -> datetime:
        return ensure_not_before(at, self.updated_at, "at")

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "intent": self.intent.to_dict(),
            "status": self.status.value,
            "selection": None if self.selection is None else self.selection.to_dict(),
            "execution": None if self.execution is None else self.execution.to_dict(),
            "attempt": self.attempt,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PlanNode:
        selection = read_optional_mapping(data, "selection")
        execution = read_optional_mapping(data, "execution")
        return cls(
            node_id=read_text(data, "node_id"),
            intent=TaskIntent.from_dict(read_mapping(data, "intent")),
            status=PlanNodeStatus(read_text(data, "status")),
            selection=None if selection is None else AgentSelection.from_dict(selection),
            execution=None if execution is None else ExecutionReference.from_dict(execution),
            attempt=read_int(data, "attempt"),
            created_at=read_datetime(data, "created_at"),
            updated_at=read_datetime(data, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    goal_id: str
    status: PlanStatus
    nodes: tuple[PlanNode, ...]
    revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", ensure_text(self.plan_id, "plan_id"))
        object.__setattr__(self, "goal_id", ensure_text(self.goal_id, "goal_id"))
        if self.revision < 0:
            raise CoordinatorDomainError("revision must not be negative")
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise CoordinatorDomainError("plan node_id values must be unique")
        task_ids = tuple(node.intent.task_id for node in self.nodes)
        if len(task_ids) != len(set(task_ids)):
            raise CoordinatorDomainError("plan task_id values must be unique")
        created_at = ensure_utc(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(
            self,
            "updated_at",
            ensure_not_before(self.updated_at, created_at, "updated_at"),
        )
        if self.status is PlanStatus.READY and any(
            node.status is not PlanNodeStatus.READY for node in self.nodes
        ):
            raise CoordinatorDomainError("ready plan requires every node to be ready")
        if self.status is PlanStatus.COMPLETED and any(
            node.status is not PlanNodeStatus.ACCEPTED for node in self.nodes
        ):
            raise CoordinatorDomainError("completed plan requires every node to be accepted")

    @classmethod
    def draft(cls, *, plan_id: str, goal_id: str, at: datetime) -> Plan:
        return cls(
            plan_id=plan_id,
            goal_id=goal_id,
            status=PlanStatus.DRAFT,
            nodes=(),
            revision=0,
            created_at=at,
            updated_at=at,
        )

    def add_node(self, node: PlanNode, *, at: datetime) -> Plan:
        self._require_status(PlanStatus.DRAFT)
        return replace(
            self,
            nodes=(*self.nodes, node),
            revision=self.revision + 1,
            updated_at=self._next_time_for_node(node, at=at),
        )

    def replace_node(self, node: PlanNode, *, at: datetime) -> Plan:
        if self.status in {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED}:
            raise InvalidTransitionError(f"plan {self.plan_id} is already {self.status}")
        match = next(
            (
                (index, current)
                for index, current in enumerate(self.nodes)
                if current.node_id == node.node_id
            ),
            None,
        )
        if match is None:
            raise CoordinatorDomainError(f"unknown plan node {node.node_id}")
        index, current = match
        if node.intent.task_id != current.intent.task_id:
            raise CoordinatorDomainError("replacement node must preserve task_id")
        nodes = list(self.nodes)
        nodes[index] = node
        return replace(
            self,
            nodes=tuple(nodes),
            revision=self.revision + 1,
            updated_at=self._next_time_for_node(node, at=at),
        )

    def mark_ready(self, *, at: datetime) -> Plan:
        self._require_status(PlanStatus.DRAFT)
        if not self.nodes:
            raise CoordinatorDomainError("plan requires at least one node")
        if any(node.status is not PlanNodeStatus.READY for node in self.nodes):
            raise CoordinatorDomainError("every plan node must be ready")
        return self._transition(PlanStatus.READY, at=at)

    def start(self, *, at: datetime) -> Plan:
        if self.status is PlanStatus.DRAFT:
            if not self.nodes or all(node.status is PlanNodeStatus.PROPOSED for node in self.nodes):
                raise CoordinatorDomainError("running plan requires at least one prepared node")
        else:
            self._require_status(PlanStatus.READY)
        return self._transition(PlanStatus.RUNNING, at=at)

    def wait(self, *, at: datetime) -> Plan:
        self._require_status(PlanStatus.RUNNING)
        return self._transition(PlanStatus.WAITING, at=at)

    def review(self, *, at: datetime) -> Plan:
        if self.status not in {PlanStatus.RUNNING, PlanStatus.WAITING}:
            raise InvalidTransitionError(f"plan {self.plan_id} cannot review from {self.status}")
        return self._transition(PlanStatus.REVIEWING, at=at)

    def resume(self, *, at: datetime) -> Plan:
        if self.status not in {PlanStatus.WAITING, PlanStatus.REVIEWING}:
            raise InvalidTransitionError(f"plan {self.plan_id} cannot resume from {self.status}")
        return self._transition(PlanStatus.RUNNING, at=at)

    def complete(self, *, at: datetime) -> Plan:
        if self.status not in {PlanStatus.RUNNING, PlanStatus.REVIEWING}:
            raise InvalidTransitionError(f"plan {self.plan_id} cannot complete from {self.status}")
        if not self.nodes or any(node.status is not PlanNodeStatus.ACCEPTED for node in self.nodes):
            raise CoordinatorDomainError("every plan node must be accepted")
        return self._transition(PlanStatus.COMPLETED, at=at)

    def fail(self, *, at: datetime) -> Plan:
        if self.status in {
            PlanStatus.DRAFT,
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
        }:
            raise InvalidTransitionError(f"plan {self.plan_id} cannot fail from {self.status}")
        return self._transition(PlanStatus.FAILED, at=at)

    def cancel(self, *, at: datetime) -> Plan:
        if self.status in {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED}:
            raise InvalidTransitionError(f"plan {self.plan_id} cannot cancel from {self.status}")
        return self._transition(PlanStatus.CANCELLED, at=at)

    def _require_status(self, status: PlanStatus) -> None:
        if self.status is not status:
            raise InvalidTransitionError(
                f"plan {self.plan_id} must be {status}, current status is {self.status}"
            )

    def _transition(self, status: PlanStatus, *, at: datetime) -> Plan:
        return replace(
            self,
            status=status,
            revision=self.revision + 1,
            updated_at=self._next_time(at),
        )

    def _next_time(self, at: datetime) -> datetime:
        return ensure_not_before(at, self.updated_at, "at")

    def _next_time_for_node(self, node: PlanNode, *, at: datetime) -> datetime:
        return ensure_not_before(at, max(self.updated_at, node.updated_at), "at")

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "goal_id": self.goal_id,
            "status": self.status.value,
            "nodes": [node.to_dict() for node in self.nodes],
            "revision": self.revision,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Plan:
        return cls(
            plan_id=read_text(data, "plan_id"),
            goal_id=read_text(data, "goal_id"),
            status=PlanStatus(read_text(data, "status")),
            nodes=tuple(PlanNode.from_dict(node) for node in read_mapping_list(data, "nodes")),
            revision=read_int(data, "revision"),
            created_at=read_datetime(data, "created_at"),
            updated_at=read_datetime(data, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    decision_id: str
    plan_id: str
    node_id: str
    kind: ReviewDecisionKind
    rationale: str
    follow_up_prompt: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "plan_id", "node_id", "rationale"):
            object.__setattr__(self, field_name, ensure_text(getattr(self, field_name), field_name))
        object.__setattr__(
            self,
            "follow_up_prompt",
            ensure_optional_text(self.follow_up_prompt, "follow_up_prompt"),
        )
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, "created_at"))
        if self.kind in {ReviewDecisionKind.REVISE, ReviewDecisionKind.RETRY} and (
            self.follow_up_prompt is None
        ):
            raise CoordinatorDomainError(f"{self.kind} decision requires follow_up_prompt")

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "plan_id": self.plan_id,
            "node_id": self.node_id,
            "kind": self.kind.value,
            "rationale": self.rationale,
            "follow_up_prompt": self.follow_up_prompt,
            "created_at": datetime_to_text(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ReviewDecision:
        return cls(
            decision_id=read_text(data, "decision_id"),
            plan_id=read_text(data, "plan_id"),
            node_id=read_text(data, "node_id"),
            kind=ReviewDecisionKind(read_text(data, "kind")),
            rationale=read_text(data, "rationale"),
            follow_up_prompt=read_optional_text(data, "follow_up_prompt"),
            created_at=read_datetime(data, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class CoordinatorEvent:
    event_id: str
    session_id: str
    event_type: CoordinatorEventType
    source: str
    occurred_at: datetime
    node_id: str | None = None
    execution: ExecutionReference | None = None
    external_event_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("event_id", "session_id", "source"):
            object.__setattr__(self, field_name, ensure_text(getattr(self, field_name), field_name))
        for field_name in ("node_id", "external_event_id"):
            object.__setattr__(
                self,
                field_name,
                ensure_optional_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at, "occurred_at"))
        if self.execution is not None and self.node_id is None:
            raise CoordinatorDomainError("execution event requires node_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "event_type": self.event_type.value,
            "source": self.source,
            "occurred_at": datetime_to_text(self.occurred_at),
            "node_id": self.node_id,
            "execution": None if self.execution is None else self.execution.to_dict(),
            "external_event_id": self.external_event_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CoordinatorEvent:
        execution = read_optional_mapping(data, "execution")
        return cls(
            event_id=read_text(data, "event_id"),
            session_id=read_text(data, "session_id"),
            event_type=CoordinatorEventType(read_text(data, "event_type")),
            source=read_text(data, "source"),
            occurred_at=read_datetime(data, "occurred_at"),
            node_id=read_optional_text(data, "node_id"),
            execution=None if execution is None else ExecutionReference.from_dict(execution),
            external_event_id=read_optional_text(data, "external_event_id"),
        )
