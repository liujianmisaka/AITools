from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from agent_framework import AgentSession

from misaka_coordinator_service.application.agent import (
    CoordinatorAgentConfig,
    CoordinatorDecisionResult,
)
from misaka_coordinator_service.application.decision import (
    CoordinatorDecision,
    CoordinatorDecisionKind,
)
from misaka_coordinator_service.domain import (
    CoordinatorSession,
    GoalStatus,
    Plan,
    PlanGraph,
    PlanNode,
    PlanNodeStatus,
)
from misaka_coordinator_service.domain._serialization import ensure_text
from misaka_coordinator_service.domain.models import PlanStatus
from misaka_coordinator_service.execution import (
    DelegationCancelRequest,
    DelegationMessageRequest,
    DelegationMode,
    DelegationReconciliationRequest,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
    ExecutionSelection,
    JsonValue,
    MessageDelivery,
    MessageDispatchSnapshot,
    ReconciliationStatus,
)


class CoordinatorOrchestrationError(RuntimeError):
    """Base error for applying a Coordinator decision to domain and V3 ports."""


class CoordinatorPlanApplicationError(CoordinatorOrchestrationError):
    """Raised when a decision cannot be applied to the current plan."""


class CoordinatorActivationOutcome(StrEnum):
    WAITING = "waiting"
    REVIEW_REQUIRED = "review_required"
    RESPONDED = "responded"
    INPUT_REQUIRED = "input_required"
    STOPPED = "stopped"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True, slots=True)
class CoordinatorOrchestratorConfig:
    workspace_root: str
    default_effort: str = "medium"
    wait_timeout_ms: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "workspace_root", ensure_text(self.workspace_root, "workspace_root")
        )
        object.__setattr__(
            self, "default_effort", ensure_text(self.default_effort, "default_effort")
        )
        if isinstance(self.wait_timeout_ms, bool) or not 0 <= self.wait_timeout_ms <= 300_000:
            raise CoordinatorOrchestrationError("wait_timeout_ms must be between 0 and 300000")


@dataclass(frozen=True, slots=True)
class CoordinatorActivationResult:
    session: CoordinatorSession
    agent_session: AgentSession
    outcome: CoordinatorActivationOutcome
    step_count: int
    message: str | None
    decisions: tuple[CoordinatorDecision, ...]
    delegations: tuple[DelegationSnapshot, ...]


class DecisionAgent(Protocol):
    @property
    def config(self) -> CoordinatorAgentConfig: ...

    async def decide(
        self,
        prompt: str,
        *,
        session: AgentSession,
        activation_id: str,
        step: int,
    ) -> CoordinatorDecisionResult: ...


class ExecutionGateway(Protocol):
    async def delegate(self, request: DelegationRequest) -> DelegationSnapshot: ...

    async def wait(self, delegation_id: str, *, timeout_ms: int) -> DelegationSnapshot: ...

    async def send_message(self, request: DelegationMessageRequest) -> MessageDispatchSnapshot: ...

    async def cancel(self, request: DelegationCancelRequest) -> DelegationSnapshot: ...

    async def resolve_reconciliation(
        self,
        request: DelegationReconciliationRequest,
    ) -> DelegationSnapshot: ...


@dataclass(frozen=True, slots=True)
class _AppliedDecision:
    session: CoordinatorSession
    outcome: CoordinatorActivationOutcome | None = None
    message: str | None = None
    delegations: tuple[DelegationSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class CoordinatorMessageResult:
    session: CoordinatorSession
    dispatch: MessageDispatchSnapshot


@dataclass(frozen=True, slots=True)
class CoordinatorCancellationResult:
    session: CoordinatorSession
    snapshot: DelegationSnapshot


@dataclass(frozen=True, slots=True)
class CoordinatorReconciliationResult:
    session: CoordinatorSession
    snapshot: DelegationSnapshot


class CoordinatorOrchestrator:
    def __init__(
        self,
        *,
        agent: DecisionAgent,
        execution: ExecutionGateway,
        config: CoordinatorOrchestratorConfig,
    ) -> None:
        self._agent = agent
        self._execution = execution
        self._config = config

    async def activate(
        self,
        prompt: str,
        *,
        session: CoordinatorSession,
        agent_session: AgentSession,
        activation_id: str,
        at: datetime,
    ) -> CoordinatorActivationResult:
        normalized_prompt = ensure_text(prompt, "prompt")
        normalized_activation_id = ensure_text(activation_id, "activation_id")
        current = session
        decisions: list[CoordinatorDecision] = []
        delegations: list[DelegationSnapshot] = []
        max_steps = self._agent.config.max_decision_steps

        for step in range(1, max_steps + 1):
            context = self._build_prompt(normalized_prompt, current, delegations)
            result = await self._agent.decide(
                context,
                session=agent_session,
                activation_id=normalized_activation_id,
                step=step,
            )
            decisions.append(result.decision)
            applied = await self._apply_decision(
                result.decision,
                session=current,
                activation_id=normalized_activation_id,
                at=at,
            )
            current = applied.session
            delegations.extend(applied.delegations)
            if applied.outcome is not None:
                return CoordinatorActivationResult(
                    session=current,
                    agent_session=agent_session,
                    outcome=applied.outcome,
                    step_count=step,
                    message=applied.message,
                    decisions=tuple(decisions),
                    delegations=tuple(delegations),
                )

        return CoordinatorActivationResult(
            session=current,
            agent_session=agent_session,
            outcome=CoordinatorActivationOutcome.LIMIT_REACHED,
            step_count=max_steps,
            message=None,
            decisions=tuple(decisions),
            delegations=tuple(delegations),
        )

    async def send_message(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        message: str,
        at: datetime,
        delivery: MessageDelivery = MessageDelivery.APPEND,
        expected_activation_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> CoordinatorMessageResult:
        current = self._ensure_plan_graph(session, at=at)
        plan, _graph = self._require_plan(current)
        node = self._find_node(plan, node_id)
        if node.execution is None:
            raise CoordinatorPlanApplicationError("message target has no execution reference")
        session_id = node.execution.worker_session_id
        if session_id is None:
            raise CoordinatorPlanApplicationError("message target has no worker session reference")
        dispatch = await self._execution.send_message(
            DelegationMessageRequest(
                delegation_id=node.execution.delegation_id,
                session_id=session_id,
                message=message,
                delivery=delivery,
                expected_activation_id=expected_activation_id or node.execution.activation_id,
                model=model,
                effort=effort,
            )
        )
        return CoordinatorMessageResult(session=current, dispatch=dispatch)

    async def continue_node(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        message: str,
        at: datetime,
        expected_activation_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> CoordinatorMessageResult:
        return await self.send_message(
            session=session,
            node_id=node_id,
            message=message,
            at=at,
            delivery=MessageDelivery.INTERRUPT_CONTINUE,
            expected_activation_id=expected_activation_id,
            model=model,
            effort=effort,
        )

    async def cancel_node(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        reason: str,
        at: datetime,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        expected_activation_id: str | None = None,
    ) -> CoordinatorCancellationResult:
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        node = self._find_node(plan, node_id)
        if node.execution is None:
            raise CoordinatorPlanApplicationError("cancel target has no execution reference")
        snapshot = await self._execution.cancel(
            DelegationCancelRequest(
                delegation_id=node.execution.delegation_id,
                reason=reason,
                request_id=request_id,
                idempotency_key=idempotency_key,
                session_id=node.execution.worker_session_id,
                expected_activation_id=expected_activation_id or node.execution.activation_id,
            )
        )
        plan = plan.replace_node(self._update_node_from_snapshot(node, snapshot, at=at), at=at)
        updated = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        return CoordinatorCancellationResult(session=updated, snapshot=snapshot)

    async def reconcile_node(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        expected_revision: int,
        status: ReconciliationStatus,
        reason: str,
        at: datetime,
        output: JsonValue = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CoordinatorReconciliationResult:
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        node = self._find_node(plan, node_id)
        if node.execution is None:
            raise CoordinatorPlanApplicationError(
                "reconciliation target has no execution reference"
            )
        snapshot = await self._execution.resolve_reconciliation(
            DelegationReconciliationRequest(
                delegation_id=node.execution.delegation_id,
                expected_revision=expected_revision,
                status=status,
                reason=reason,
                output=output,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )
        )
        plan = plan.replace_node(self._update_node_from_snapshot(node, snapshot, at=at), at=at)
        updated = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        return CoordinatorReconciliationResult(session=updated, snapshot=snapshot)

    async def _apply_decision(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        activation_id: str,
        at: datetime,
    ) -> _AppliedDecision:
        if decision.kind is CoordinatorDecisionKind.CREATE_PLAN:
            return self._create_plan(decision, session=session, at=at)
        if decision.kind is CoordinatorDecisionKind.DELEGATE:
            return await self._delegate(
                decision, session=session, activation_id=activation_id, at=at
            )
        if decision.kind is CoordinatorDecisionKind.WAIT:
            return await self._wait(decision, session=session, at=at)
        if decision.kind is CoordinatorDecisionKind.REVIEW:
            return self._review(decision, session=session, at=at)
        if decision.kind is CoordinatorDecisionKind.RESPOND:
            return _AppliedDecision(
                session=session,
                outcome=CoordinatorActivationOutcome.RESPONDED,
                message=decision.message,
            )
        if decision.kind is CoordinatorDecisionKind.REQUEST_INPUT:
            return _AppliedDecision(
                session=session,
                outcome=CoordinatorActivationOutcome.INPUT_REQUIRED,
                message=decision.message,
            )
        return _AppliedDecision(session=session, outcome=CoordinatorActivationOutcome.STOPPED)

    def _create_plan(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
    ) -> _AppliedDecision:
        if session.goal is None or session.goal.status is not GoalStatus.ACTIVE:
            raise CoordinatorPlanApplicationError("create_plan requires an active goal")
        plan_id = f"plan-{decision.decision_id}"
        if session.plan is not None:
            if session.plan.plan_id == plan_id:
                return _AppliedDecision(session=session)
            raise CoordinatorPlanApplicationError("session already has a different plan")
        plan = Plan.draft(plan_id=plan_id, goal_id=session.goal.goal_id, at=at)
        graph = PlanGraph.empty(plan_id=plan_id, at=at)
        for task in decision.tasks:
            plan = plan.add_node(
                PlanNode.propose(node_id=task.task_id, intent=task, at=at),
                at=at,
            )
        task_ids = {task.task_id for task in decision.tasks}
        for task in decision.tasks:
            if task.parent_task_id is not None:
                if task.parent_task_id not in task_ids:
                    raise CoordinatorPlanApplicationError(
                        f"task {task.task_id} references an unknown parent task"
                    )
                graph = graph.add_dependency(
                    node_id=task.task_id,
                    depends_on_node_id=task.parent_task_id,
                    at=at,
                )
        if decision.selection is not None:
            for node in plan.nodes:
                plan = plan.replace_node(node.select(decision.selection, at=at), at=at)
            plan = plan.mark_ready(at=at)
        return _AppliedDecision(
            session=session.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        )

    async def _delegate(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        activation_id: str,
        at: datetime,
    ) -> _AppliedDecision:
        if len(decision.tasks) != 1 or decision.selection is None:
            raise CoordinatorPlanApplicationError(
                "delegate requires one task and an agent selection"
            )
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        task = decision.tasks[0]
        node = self._find_node(plan, decision.target_node_id or task.task_id)
        if node.intent.task_id != task.task_id:
            raise CoordinatorPlanApplicationError("delegate target does not match the task")
        if node.status is PlanNodeStatus.FAILED:
            node = node.retry(at=at, selection=decision.selection)
        elif node.status is PlanNodeStatus.PROPOSED:
            node = node.select(decision.selection, at=at)
        elif node.status is not PlanNodeStatus.READY:
            raise CoordinatorPlanApplicationError(
                f"node {node.node_id} cannot be delegated from {node.status}"
            )
        if node.selection is None:
            raise CoordinatorPlanApplicationError(f"node {node.node_id} has no agent selection")
        plan = plan.replace_node(node, at=at)
        if node.node_id not in graph.ready_node_ids(plan):
            raise CoordinatorPlanApplicationError(
                f"node {node.node_id} is blocked by plan dependencies"
            )
        if plan.status is PlanStatus.DRAFT:
            plan = plan.start(at=at)
        elif plan.status is PlanStatus.READY:
            plan = plan.start(at=at)
        elif plan.status in {PlanStatus.WAITING, PlanStatus.REVIEWING}:
            plan = plan.resume(at=at)
        elif plan.status in {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED}:
            raise CoordinatorPlanApplicationError(f"plan {plan.plan_id} is already {plan.status}")
        parent_delegation_id = self._parent_delegation_id(plan, node)
        selection = ExecutionSelection(
            provider_id=node.selection.provider_id,
            model=node.selection.model_id,
            effort=node.selection.effort or self._config.default_effort,
        )
        request = DelegationRequest(
            prompt=node.intent.objective,
            cwd=self._config.workspace_root,
            selection=selection,
            mode=DelegationMode.CONTINUABLE,
            idempotency_key=f"{current.session_id}:{node.node_id}:attempt-{node.attempt}",
            session_id=f"{current.session_id}:{node.node_id}",
            channel_id=f"channel:{current.session_id}:{node.node_id}",
            parent_delegation_id=parent_delegation_id,
            required_features=node.intent.required_capabilities,
            decision_ref={"decision_id": decision.decision_id, "activation_id": activation_id},
            input={
                "acceptance_criteria": list(node.intent.acceptance_criteria),
                "constraints": list(node.intent.constraints),
            },
        )
        snapshot = await self._execution.delegate(request)
        node = self._bind_snapshot(node, snapshot, at=at)
        plan = plan.replace_node(node, at=at)
        updated = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        return _AppliedDecision(session=updated, delegations=(snapshot,))

    async def _wait(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
    ) -> _AppliedDecision:
        if decision.target_node_id is None:
            return _AppliedDecision(session=session, outcome=CoordinatorActivationOutcome.WAITING)
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        node = self._find_node(plan, decision.target_node_id)
        if node.execution is None:
            raise CoordinatorPlanApplicationError("wait target has no execution reference")
        snapshot = await self._execution.wait(
            node.execution.delegation_id,
            timeout_ms=self._config.wait_timeout_ms,
        )
        node = self._update_node_from_snapshot(node, snapshot, at=at)
        plan = plan.replace_node(node, at=at)
        if snapshot.status is not DelegationStatus.COMPLETED and plan.status is PlanStatus.RUNNING:
            plan = plan.wait(at=at)
        updated = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        outcome = (
            CoordinatorActivationOutcome.REVIEW_REQUIRED
            if node.status is PlanNodeStatus.REVIEW_REQUIRED
            else CoordinatorActivationOutcome.WAITING
        )
        return _AppliedDecision(session=updated, outcome=outcome, delegations=(snapshot,))

    def _review(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
    ) -> _AppliedDecision:
        if decision.target_node_id is None:
            raise CoordinatorPlanApplicationError("review requires target_node_id")
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        node = self._find_node(plan, decision.target_node_id)
        if node.status in {PlanNodeStatus.DELEGATED, PlanNodeStatus.AWAITING_EVENT}:
            node = node.request_review(at=at)
            plan = plan.replace_node(node, at=at)
        if plan.status is PlanStatus.RUNNING:
            plan = plan.review(at=at)
        updated = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        return _AppliedDecision(
            session=updated,
            outcome=CoordinatorActivationOutcome.REVIEW_REQUIRED,
        )

    def _ensure_plan_graph(
        self, session: CoordinatorSession, *, at: datetime
    ) -> CoordinatorSession:
        if session.plan is None:
            raise CoordinatorPlanApplicationError("decision requires an existing plan")
        if session.plan_graph is not None:
            return session
        return session.attach_plan_graph(
            PlanGraph.empty(plan_id=session.plan.plan_id, at=at), at=at
        )

    @staticmethod
    def _require_plan(session: CoordinatorSession) -> tuple[Plan, PlanGraph]:
        if session.plan is None or session.plan_graph is None:
            raise CoordinatorPlanApplicationError("session plan and plan graph are required")
        return session.plan, session.plan_graph

    @staticmethod
    def _find_node(plan: Plan, node_id: str) -> PlanNode:
        normalized_node_id = ensure_text(node_id, "node_id")
        for node in plan.nodes:
            if node.node_id == normalized_node_id or node.intent.task_id == normalized_node_id:
                return node
        raise CoordinatorPlanApplicationError(f"unknown plan node {normalized_node_id}")

    @staticmethod
    def _parent_delegation_id(plan: Plan, node: PlanNode) -> str | None:
        parent_task_id = node.intent.parent_task_id
        if parent_task_id is None:
            return None
        parent = next(
            (candidate for candidate in plan.nodes if candidate.intent.task_id == parent_task_id),
            None,
        )
        if parent is None or parent.execution is None:
            return None
        return parent.execution.delegation_id

    @staticmethod
    def _bind_snapshot(node: PlanNode, snapshot: DelegationSnapshot, *, at: datetime) -> PlanNode:
        bound = node.bind_execution(snapshot.execution_reference, at=at)
        if snapshot.status in {
            DelegationStatus.COMPLETED,
            DelegationStatus.RECONCILIATION_REQUIRED,
        }:
            return bound.request_review(at=at)
        if snapshot.status in {DelegationStatus.FAILED, DelegationStatus.REJECTED}:
            return bound.fail(at=at)
        if snapshot.status is DelegationStatus.CANCELLED:
            return bound.cancel(at=at)
        return bound.await_event(at=at)

    @staticmethod
    def _update_node_from_snapshot(
        node: PlanNode,
        snapshot: DelegationSnapshot,
        *,
        at: datetime,
    ) -> PlanNode:
        if node.execution != snapshot.execution_reference:
            node = replace(node, execution=snapshot.execution_reference)
        if snapshot.status in {
            DelegationStatus.COMPLETED,
            DelegationStatus.RECONCILIATION_REQUIRED,
        }:
            if node.status in {PlanNodeStatus.DELEGATED, PlanNodeStatus.AWAITING_EVENT}:
                return node.request_review(at=at)
            return node
        if snapshot.status in {DelegationStatus.FAILED, DelegationStatus.REJECTED}:
            if node.status not in {
                PlanNodeStatus.FAILED,
                PlanNodeStatus.CANCELLED,
                PlanNodeStatus.ACCEPTED,
            }:
                return node.fail(at=at)
            return node
        if snapshot.status is DelegationStatus.CANCELLED:
            if node.status not in {
                PlanNodeStatus.FAILED,
                PlanNodeStatus.CANCELLED,
                PlanNodeStatus.ACCEPTED,
            }:
                return node.cancel(at=at)
            return node
        if node.status is PlanNodeStatus.DELEGATED:
            return node.await_event(at=at)
        return node

    @staticmethod
    def _build_prompt(
        prompt: str,
        session: CoordinatorSession,
        delegations: Sequence[DelegationSnapshot],
    ) -> str:
        facts = {
            "input": prompt,
            "session": session.to_dict(),
            "delegations": [
                {
                    "delegation_id": snapshot.delegation_id,
                    "status": snapshot.status.value,
                    "revision": snapshot.revision,
                    "session_id": snapshot.session_id,
                    "current_activation_id": snapshot.current_activation_id,
                    "current_invocation_id": snapshot.current_invocation_id,
                    "next_action": snapshot.next_action,
                }
                for snapshot in delegations
            ],
        }
        return "Apply one next orchestration decision using these current facts: " + json.dumps(
            facts,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
