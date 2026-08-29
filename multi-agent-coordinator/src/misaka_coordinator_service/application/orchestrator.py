from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from agent_framework import AgentSession

from misaka_coordinator_service.application.agent import (
    CoordinatorAgentConfig,
    CoordinatorDecisionResult,
)
from misaka_coordinator_service.application.autonomy import (
    CoordinatorAutonomyPolicy,
    CoordinatorPolicyApprovalRequired,
)
from misaka_coordinator_service.application.decision import (
    CoordinatorDecision,
    CoordinatorDecisionKind,
)
from misaka_coordinator_service.domain import (
    CoordinatorSession,
    GoalStatus,
    Plan,
    PlanDependency,
    PlanGraph,
    PlanNode,
    PlanNodeStatus,
    PlanRevision,
    PlanStatus,
)
from misaka_coordinator_service.domain._serialization import ensure_text
from misaka_coordinator_service.execution import (
    DelegationCancelRequest,
    DelegationMessageRequest,
    DelegationMode,
    DelegationReconciliationRequest,
    DelegationRequest,
    DelegationSessionEvent,
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
    workspace_root: str | None = None
    default_effort: str = "medium"
    wait_timeout_ms: int = 0
    autonomy_policy: CoordinatorAutonomyPolicy = field(default_factory=CoordinatorAutonomyPolicy)

    def __post_init__(self) -> None:
        if self.workspace_root is not None:
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


@dataclass(frozen=True, slots=True)
class CoordinatorNodeResult:
    session: CoordinatorSession


@dataclass(frozen=True, slots=True)
class CoordinatorRetryResult:
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
        cwd: str | None = None,
    ) -> CoordinatorActivationResult:
        normalized_prompt = ensure_text(prompt, "prompt")
        normalized_activation_id = ensure_text(activation_id, "activation_id")
        current = session
        decisions: list[CoordinatorDecision] = []
        delegations: list[DelegationSnapshot] = []
        max_steps = self._agent.config.max_decision_steps
        activation_authorization = self._config.autonomy_policy.authorize(
            current,
            self._config.autonomy_policy.activation_requirements(current, at=at),
            at=at,
        )
        current = activation_authorization.session
        if activation_authorization.blocked_approval is not None:
            return CoordinatorActivationResult(
                session=current,
                agent_session=agent_session,
                outcome=CoordinatorActivationOutcome.INPUT_REQUIRED,
                step_count=0,
                message=activation_authorization.blocked_approval.reason,
                decisions=(),
                delegations=(),
            )

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
                at=at,
                cwd=cwd,
            )
            current = applied.session
            delegations.extend(applied.delegations)
            if applied.outcome is not None:
                current = self._config.autonomy_policy.record_action(
                    current,
                    activation_authorization.approvals,
                    at=at,
                    model_activation=True,
                )
                return CoordinatorActivationResult(
                    session=current,
                    agent_session=agent_session,
                    outcome=applied.outcome,
                    step_count=step,
                    message=applied.message,
                    decisions=tuple(decisions),
                    delegations=tuple(delegations),
                )

        current = self._config.autonomy_policy.record_action(
            current,
            activation_authorization.approvals,
            at=at,
            model_activation=True,
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

    def observe_snapshot(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        snapshot: DelegationSnapshot,
        at: datetime,
    ) -> CoordinatorSession:
        """Apply a V3 snapshot to the Coordinator plan without creating a new delegation."""

        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        node = self._find_node(plan, node_id)
        if node.execution is None:
            raise CoordinatorPlanApplicationError("snapshot target has no execution reference")
        if node.execution.delegation_id != snapshot.delegation_id:
            raise CoordinatorPlanApplicationError("snapshot delegation_id does not match the node")
        updated_node = self._update_node_from_snapshot(node, snapshot, at=at)
        if updated_node == node:
            return current
        updated_plan = plan.replace_node(updated_node, at=at)
        return current.attach_plan(updated_plan, at=at).attach_plan_graph(graph, at=at)

    def observe_event(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        source_event: DelegationSessionEvent,
        at: datetime,
    ) -> CoordinatorSession:
        """Apply a V3 event's terminal status without creating a new delegation."""

        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        node = self._find_node(plan, node_id)
        if node.execution is None:
            raise CoordinatorPlanApplicationError("event target has no execution reference")
        if node.execution.delegation_id != source_event.delegation_id:
            raise CoordinatorPlanApplicationError("event delegation_id does not match the node")
        updated_node = self._update_node_from_event(node, source_event, at=at)
        if updated_node == node:
            return current
        updated_plan = plan.replace_node(updated_node, at=at)
        return current.attach_plan(updated_plan, at=at).attach_plan_graph(graph, at=at)

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
        authorization = self._config.autonomy_policy.authorize(
            current,
            self._config.autonomy_policy.reconciliation_requirements(
                session=current,
                node=node,
                expected_revision=expected_revision,
            ),
            at=at,
        )
        if authorization.blocked_approval is not None:
            raise CoordinatorPolicyApprovalRequired(
                session=authorization.session,
                approval=authorization.blocked_approval,
            )
        current = authorization.session
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
        updated = self._config.autonomy_policy.record_action(
            updated,
            authorization.approvals,
            at=at,
        )
        return CoordinatorReconciliationResult(session=updated, snapshot=snapshot)

    async def accept_result(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        at: datetime,
    ) -> CoordinatorNodeResult:
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        node = self._find_node(plan, node_id)
        if node.execution is None:
            raise CoordinatorPlanApplicationError("accept target has no execution reference")
        snapshot = await self._execution.wait(node.execution.delegation_id, timeout_ms=0)
        if snapshot.status is not DelegationStatus.COMPLETED:
            raise CoordinatorPlanApplicationError(
                f"node {node.node_id} cannot be accepted while V3 status is {snapshot.status}"
            )
        if node.status is PlanNodeStatus.RECONCILIATION_REQUIRED:
            node = node.request_review(at=at)
        plan = self._accept_node(plan, node, at=at)
        updated = current.attach_plan(plan, at=at)
        return CoordinatorNodeResult(session=updated.attach_plan_graph(graph, at=at))

    async def retry_node(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
        at: datetime,
        cwd: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> CoordinatorRetryResult:
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        node = self._find_node(plan, node_id)
        if node.selection is None:
            raise CoordinatorPlanApplicationError("retry target has no agent selection")
        if node.execution is None:
            raise CoordinatorPlanApplicationError("retry target has no execution reference")
        snapshot = await self._execution.wait(node.execution.delegation_id, timeout_ms=0)
        if snapshot.status is DelegationStatus.RECONCILIATION_REQUIRED:
            raise CoordinatorPlanApplicationError(
                f"node {node.node_id} must be reconciled before retry"
            )
        if snapshot.status not in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
            DelegationStatus.CANCELLED,
        }:
            raise CoordinatorPlanApplicationError(
                f"node {node.node_id} cannot retry while V3 status is {snapshot.status}"
            )
        if node.status is PlanNodeStatus.RECONCILIATION_REQUIRED:
            if snapshot.status is DelegationStatus.COMPLETED:
                node = node.request_review(at=at)
            else:
                node = node.fail(at=at)
            plan = plan.replace_node(node, at=at)
            current = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        selection = node.selection
        if selection is None:
            raise CoordinatorPlanApplicationError("retry target has no agent selection")
        if model is not None or effort is not None:
            if model is None or effort is None:
                raise CoordinatorPlanApplicationError("model and effort must be provided together")
            selection = replace(selection, model_id=model, effort=effort)
        if node.status not in {
            PlanNodeStatus.FAILED,
            PlanNodeStatus.REVIEW_REQUIRED,
        }:
            raise CoordinatorPlanApplicationError(
                f"node {node.node_id} cannot retry from {node.status}"
            )
        applied = await self._delegate(
            CoordinatorDecision(
                decision_id=f"retry:{node.node_id}:{node.attempt + 1}",
                kind=CoordinatorDecisionKind.DELEGATE,
                rationale="manual node retry",
                tasks=(node.intent,),
                selection=selection,
                target_node_id=node.node_id,
                message=None,
            ),
            session=current,
            at=at,
            cwd=cwd,
        )
        if len(applied.delegations) != 1:
            raise CoordinatorPlanApplicationError("retry did not create exactly one delegation")
        return CoordinatorRetryResult(session=applied.session, snapshot=applied.delegations[0])

    async def inspect_node(
        self,
        *,
        session: CoordinatorSession,
        node_id: str,
    ) -> DelegationSnapshot:
        if session.plan is None:
            raise CoordinatorPlanApplicationError("inspection requires an existing plan")
        node = self._find_node(session.plan, node_id)
        if node.execution is None:
            raise CoordinatorPlanApplicationError("inspection target has no execution reference")
        return await self._execution.wait(node.execution.delegation_id, timeout_ms=0)

    async def _apply_decision(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
        cwd: str | None,
    ) -> _AppliedDecision:
        if decision.kind is CoordinatorDecisionKind.CREATE_PLAN:
            return self._create_plan(decision, session=session, at=at)
        if decision.kind is CoordinatorDecisionKind.REVISE_PLAN:
            return self._revise_plan(decision, session=session, at=at)
        if decision.kind is CoordinatorDecisionKind.DELEGATE:
            return await self._delegate(decision, session=session, at=at, cwd=cwd)
        if decision.kind is CoordinatorDecisionKind.DISPATCH_READY:
            return await self._dispatch_ready(decision, session=session, at=at, cwd=cwd)
        if decision.kind is CoordinatorDecisionKind.SEND_MESSAGE:
            return await self._send_message_decision(decision, session=session, at=at)
        if decision.kind is CoordinatorDecisionKind.CANCEL_DELEGATION:
            return await self._cancel_decision(decision, session=session, at=at)
        if decision.kind is CoordinatorDecisionKind.WAIT:
            return await self._wait(decision, session=session, at=at)
        if decision.kind is CoordinatorDecisionKind.REVIEW:
            return self._review(decision, session=session, at=at)
        if decision.kind is CoordinatorDecisionKind.ACCEPT_RESULT:
            return self._accept_result(decision, session=session, at=at)
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
        if decision.kind is CoordinatorDecisionKind.COMPLETE_GOAL:
            return self._complete_goal(decision, session=session, at=at)
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
        goal_id = session.goal.goal_id
        goal_objective = session.goal.objective
        plan_id = f"plan-{decision.decision_id}"
        if session.plan is not None:
            if session.plan.plan_id == plan_id:
                return _AppliedDecision(session=session)
            raise CoordinatorPlanApplicationError("session already has a different plan")
        authorization = self._config.autonomy_policy.authorize(
            session,
            self._config.autonomy_policy.plan_revision_requirements(session),
            at=at,
        )
        if authorization.blocked_approval is not None:
            return _AppliedDecision(
                session=authorization.session,
                outcome=CoordinatorActivationOutcome.INPUT_REQUIRED,
                message=authorization.blocked_approval.reason,
            )
        session = authorization.session
        plan = Plan.draft(plan_id=plan_id, goal_id=goal_id, at=at)
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
        updated = session.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        plan_revision = PlanRevision.create(
            plan_id=plan.plan_id,
            objective=goal_objective,
            rationale=decision.rationale,
            tasks=tuple(node.intent for node in plan.nodes),
            previous=None,
            at=at,
        )
        updated = updated.append_plan_revision(plan_revision, at=at)
        updated = self._config.autonomy_policy.record_action(
            updated,
            authorization.approvals,
            at=at,
            plan_revision=True,
        )
        return _AppliedDecision(session=updated)

    def _revise_plan(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
    ) -> _AppliedDecision:
        if session.goal is None or session.goal.status is not GoalStatus.ACTIVE:
            raise CoordinatorPlanApplicationError("revise_plan requires an active goal")
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        if plan.status in {PlanStatus.COMPLETED, PlanStatus.CANCELLED}:
            raise CoordinatorPlanApplicationError(f"plan {plan.plan_id} is already {plan.status}")
        authorization = self._config.autonomy_policy.authorize(
            current,
            self._config.autonomy_policy.plan_revision_requirements(current),
            at=at,
        )
        if authorization.blocked_approval is not None:
            return _AppliedDecision(
                session=authorization.session,
                outcome=CoordinatorActivationOutcome.INPUT_REQUIRED,
                message=authorization.blocked_approval.reason,
            )
        current = authorization.session
        existing_by_task = {node.intent.task_id: node for node in plan.nodes}
        revised_nodes: list[PlanNode] = []
        revised_task_ids: set[str] = set()
        for task in decision.tasks:
            revised_task_ids.add(task.task_id)
            existing = existing_by_task.get(task.task_id)
            if existing is not None and existing.execution is not None:
                if existing.intent != task:
                    raise CoordinatorPlanApplicationError(
                        f"executed task {task.task_id} cannot be changed by plan revision"
                    )
                revised_nodes.append(existing)
                continue
            if existing is not None and existing.intent == task:
                node = existing
                if node.status is PlanNodeStatus.PROPOSED and decision.selection is not None:
                    node = node.select(decision.selection, at=at)
                elif (
                    node.status is PlanNodeStatus.READY
                    and decision.selection is not None
                    and node.selection != decision.selection
                ):
                    node = replace(node, selection=decision.selection, updated_at=at)
                revised_nodes.append(node)
                continue
            node = PlanNode.propose(node_id=task.task_id, intent=task, at=at)
            if decision.selection is not None:
                node = node.select(decision.selection, at=at)
            revised_nodes.append(node)
        revised_nodes.extend(
            node
            for node in plan.nodes
            if node.execution is not None and node.intent.task_id not in revised_task_ids
        )
        node_ids = {node.intent.task_id for node in revised_nodes}
        dependencies: list[PlanDependency] = []
        for node in revised_nodes:
            parent_task_id = node.intent.parent_task_id
            if parent_task_id is None:
                continue
            if parent_task_id not in node_ids:
                raise CoordinatorPlanApplicationError(
                    f"task {node.intent.task_id} references an unknown parent task"
                )
            dependencies.append(
                PlanDependency(
                    node_id=node.node_id,
                    depends_on_node_id=parent_task_id,
                )
            )
        revised_plan = Plan(
            plan_id=plan.plan_id,
            goal_id=plan.goal_id,
            status=self._revised_plan_status(tuple(revised_nodes)),
            nodes=tuple(revised_nodes),
            revision=plan.revision + 1,
            created_at=plan.created_at,
            updated_at=at,
        )
        revised_graph = PlanGraph(
            plan_id=graph.plan_id,
            dependencies=tuple(dependencies),
            revision=graph.revision + 1,
            created_at=graph.created_at,
            updated_at=at,
        )
        previous_revision = current.plan_revisions[-1] if current.plan_revisions else None
        plan_revision = PlanRevision.create(
            plan_id=plan.plan_id,
            objective=session.goal.objective,
            rationale=decision.rationale,
            tasks=tuple(node.intent for node in revised_nodes),
            previous=previous_revision,
            at=at,
        )
        updated = current.apply_plan_revision(
            plan=revised_plan,
            plan_graph=revised_graph,
            plan_revision=plan_revision,
            at=at,
        )
        updated = self._config.autonomy_policy.record_action(
            updated,
            authorization.approvals,
            at=at,
            plan_revision=True,
        )
        return _AppliedDecision(session=updated)

    async def _dispatch_ready(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
        cwd: str | None,
    ) -> _AppliedDecision:
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        proposed_nodes = tuple(
            node for node in plan.nodes if node.status is PlanNodeStatus.PROPOSED
        )
        if proposed_nodes:
            if decision.selection is None:
                return _AppliedDecision(
                    session=current,
                    outcome=CoordinatorActivationOutcome.WAITING,
                    message="proposed plan nodes require an agent selection before dispatch",
                )
            for node in proposed_nodes:
                plan = plan.replace_node(node.select(decision.selection, at=at), at=at)
            if plan.status is PlanStatus.DRAFT:
                plan = plan.mark_ready(at=at)
            current = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        ready_node_ids = graph.ready_node_ids(plan)
        if not ready_node_ids:
            return _AppliedDecision(
                session=current,
                outcome=CoordinatorActivationOutcome.WAITING,
                message="no plan node is currently ready to dispatch",
            )
        delegations: list[DelegationSnapshot] = []
        for node_id in ready_node_ids:
            current_plan, _current_graph = self._require_plan(current)
            node = self._find_node(current_plan, node_id)
            if node.selection is None:
                raise CoordinatorPlanApplicationError(
                    f"ready node {node.node_id} has no agent selection"
                )
            applied = await self._delegate(
                CoordinatorDecision(
                    decision_id=f"{decision.decision_id}:{node.node_id}",
                    kind=CoordinatorDecisionKind.DELEGATE,
                    rationale=decision.rationale,
                    tasks=(node.intent,),
                    selection=node.selection,
                    target_node_id=node.node_id,
                    message=None,
                ),
                session=current,
                at=at,
                cwd=cwd,
            )
            current = applied.session
            delegations.extend(applied.delegations)
            if applied.outcome is not None:
                return _AppliedDecision(
                    session=current,
                    outcome=applied.outcome,
                    message=applied.message,
                    delegations=tuple(delegations),
                )
        return _AppliedDecision(session=current, delegations=tuple(delegations))

    async def _send_message_decision(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
    ) -> _AppliedDecision:
        if decision.target_node_id is None or decision.message is None:
            raise CoordinatorPlanApplicationError(
                "send_message requires target_node_id and message"
            )
        result = await self.send_message(
            session=session,
            node_id=decision.target_node_id,
            message=decision.message,
            at=at,
        )
        return _AppliedDecision(session=result.session)

    async def _cancel_decision(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
    ) -> _AppliedDecision:
        if decision.target_node_id is None or decision.message is None:
            raise CoordinatorPlanApplicationError(
                "cancel_delegation requires target_node_id and message"
            )
        result = await self.cancel_node(
            session=session,
            node_id=decision.target_node_id,
            reason=decision.message,
            at=at,
        )
        return _AppliedDecision(session=result.session, delegations=(result.snapshot,))

    async def _delegate(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
        cwd: str | None,
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
        if node.status in {
            PlanNodeStatus.FAILED,
            PlanNodeStatus.REVIEW_REQUIRED,
        }:
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
        execution_cwd = cwd or self._config.workspace_root
        if execution_cwd is None:
            raise CoordinatorPlanApplicationError("delegate requires cwd")
        authorization = self._config.autonomy_policy.authorize(
            current,
            self._config.autonomy_policy.delegation_requirements(
                session=current, node=node, cwd=execution_cwd
            ),
            at=at,
        )
        if authorization.blocked_approval is not None:
            return _AppliedDecision(
                session=authorization.session,
                outcome=CoordinatorActivationOutcome.INPUT_REQUIRED,
                message=authorization.blocked_approval.reason,
            )
        current = authorization.session
        selection = ExecutionSelection(
            provider_id=node.selection.provider_id,
            model=node.selection.model_id,
            effort=node.selection.effort or self._config.default_effort,
        )
        idempotency_key = f"{current.session_id}:{node.node_id}:attempt-{node.attempt}"
        delegation_id = (
            "delegation-coordinator-"
            f"{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()[:32]}"
        )
        request = DelegationRequest(
            prompt=node.intent.objective,
            cwd=ensure_text(execution_cwd, "cwd"),
            selection=selection,
            mode=DelegationMode.CONTINUABLE,
            delegation_id=delegation_id,
            idempotency_key=idempotency_key,
            session_id=f"{current.session_id}:{node.node_id}",
            channel_id=f"channel:{current.session_id}:{node.node_id}",
            parent_delegation_id=parent_delegation_id,
            input={
                "acceptance_criteria": list(node.intent.acceptance_criteria),
                "constraints": list(node.intent.constraints),
            },
        )
        snapshot = await self._execution.delegate(request)
        node = self._bind_snapshot(node, snapshot, at=at)
        plan = plan.replace_node(node, at=at)
        updated = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        updated = self._config.autonomy_policy.record_action(
            updated,
            authorization.approvals,
            at=at,
            delegation=True,
        )
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
        if node.status is PlanNodeStatus.REVIEW_REQUIRED:
            return _AppliedDecision(session=current)
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

    def _accept_result(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
    ) -> _AppliedDecision:
        if decision.target_node_id is None:
            raise CoordinatorPlanApplicationError("accept_result requires target_node_id")
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        node = self._find_node(plan, decision.target_node_id)
        plan = self._accept_node(plan, node, at=at)
        updated = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        return _AppliedDecision(session=updated)

    def _complete_goal(
        self,
        decision: CoordinatorDecision,
        *,
        session: CoordinatorSession,
        at: datetime,
    ) -> _AppliedDecision:
        if session.goal is None:
            raise CoordinatorPlanApplicationError("complete_goal requires a goal")
        if session.goal.status is not GoalStatus.ACTIVE:
            return _AppliedDecision(
                session=session,
                outcome=CoordinatorActivationOutcome.STOPPED,
                message=decision.message,
            )
        current = self._ensure_plan_graph(session, at=at)
        plan, graph = self._require_plan(current)
        if plan.status is not PlanStatus.COMPLETED:
            if plan.status is PlanStatus.WAITING:
                plan = plan.resume(at=at)
            plan = plan.complete(at=at)
            current = current.attach_plan(plan, at=at).attach_plan_graph(graph, at=at)
        completed = current.complete_goal(at=at)
        return _AppliedDecision(
            session=completed,
            outcome=CoordinatorActivationOutcome.STOPPED,
            message=decision.message,
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
    def _revised_plan_status(nodes: tuple[PlanNode, ...]) -> PlanStatus:
        if not nodes:
            raise CoordinatorPlanApplicationError("revised plan requires at least one node")
        statuses = {node.status for node in nodes}
        if statuses == {PlanNodeStatus.ACCEPTED}:
            return PlanStatus.COMPLETED
        if PlanNodeStatus.REVIEW_REQUIRED in statuses:
            return PlanStatus.REVIEWING
        if statuses == {PlanNodeStatus.PROPOSED}:
            return PlanStatus.DRAFT
        if statuses == {PlanNodeStatus.READY}:
            return PlanStatus.READY
        return PlanStatus.RUNNING

    @staticmethod
    def _accept_node(plan: Plan, node: PlanNode, *, at: datetime) -> Plan:
        updated = plan.replace_node(node.accept(at=at), at=at)
        statuses = {candidate.status for candidate in updated.nodes}
        if statuses == {PlanNodeStatus.ACCEPTED}:
            if updated.status is PlanStatus.WAITING:
                updated = updated.resume(at=at)
            updated = updated.complete(at=at)
        elif updated.status is PlanStatus.REVIEWING and not statuses.intersection(
            {
                PlanNodeStatus.RECONCILIATION_REQUIRED,
                PlanNodeStatus.REVIEW_REQUIRED,
                PlanNodeStatus.FAILED,
                PlanNodeStatus.CANCELLED,
            }
        ):
            updated = updated.resume(at=at)
        return updated

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
        if snapshot.status is DelegationStatus.COMPLETED:
            return bound.request_review(at=at)
        if snapshot.status is DelegationStatus.RECONCILIATION_REQUIRED:
            return bound.request_reconciliation(at=at)
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
        if snapshot.status is DelegationStatus.COMPLETED:
            if node.status in {
                PlanNodeStatus.DELEGATED,
                PlanNodeStatus.AWAITING_EVENT,
                PlanNodeStatus.RECONCILIATION_REQUIRED,
            }:
                return node.request_review(at=at)
            return node
        if snapshot.status is DelegationStatus.RECONCILIATION_REQUIRED:
            if node.status in {
                PlanNodeStatus.DELEGATED,
                PlanNodeStatus.AWAITING_EVENT,
                PlanNodeStatus.REVIEW_REQUIRED,
            }:
                return node.request_reconciliation(at=at)
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
    def _update_node_from_event(
        node: PlanNode,
        source_event: DelegationSessionEvent,
        *,
        at: datetime,
    ) -> PlanNode:
        status = (source_event.status or "").strip().lower()
        kind = source_event.kind.strip().lower()
        if status == "completed" or kind == "completed":
            if node.status in {PlanNodeStatus.DELEGATED, PlanNodeStatus.AWAITING_EVENT}:
                return node.request_review(at=at)
            return node
        if status == "reconciliation_required" or kind == "reconciliation_required":
            if node.status in {PlanNodeStatus.DELEGATED, PlanNodeStatus.AWAITING_EVENT}:
                return node.request_reconciliation(at=at)
            return node
        if status in {"failed", "rejected"} or kind in {"failed", "rejected"}:
            if node.status not in {
                PlanNodeStatus.FAILED,
                PlanNodeStatus.CANCELLED,
                PlanNodeStatus.ACCEPTED,
            }:
                return node.fail(at=at)
            return node
        if status == "cancelled" or kind == "cancelled":
            if node.status not in {
                PlanNodeStatus.FAILED,
                PlanNodeStatus.CANCELLED,
                PlanNodeStatus.ACCEPTED,
            }:
                return node.cancel(at=at)
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
