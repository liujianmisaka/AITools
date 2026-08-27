import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from agent_framework import AgentSession

from misaka_coordinator_service.application import (
    CoordinatorActivationOutcome,
    CoordinatorAgentConfig,
    CoordinatorDecision,
    CoordinatorDecisionKind,
    CoordinatorDecisionResult,
    CoordinatorOrchestrationError,
    CoordinatorOrchestrator,
    CoordinatorOrchestratorConfig,
    CoordinatorPlanApplicationError,
    CoordinatorPolicyApprovalRequired,
    CoordinatorReasoningEffort,
)
from misaka_coordinator_service.domain import (
    AgentSelection,
    CoordinatorSession,
    Goal,
    GoalStatus,
    PlanNodeStatus,
    PlanStatus,
    TaskIntent,
)
from misaka_coordinator_service.execution import (
    DelegationCancelRequest,
    DelegationMessageRequest,
    DelegationReconciliationRequest,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
    MessageDispatchSnapshot,
    ReconciliationStatus,
)

BASE_TIME = datetime(2026, 8, 27, 8, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


def make_session() -> CoordinatorSession:
    goal = Goal(
        goal_id="goal-1",
        objective="完成委派",
        acceptance_criteria=("完成子任务",),
        constraints=("禁止 Push",),
        status=GoalStatus.ACTIVE,
        created_at=at(0),
        updated_at=at(0),
    )
    return CoordinatorSession.create(
        session_id="coordinator-session-1",
        cognitive_session_id="maf-session-1",
        at=at(0),
    ).start_goal(goal, at=at(1))


def task(task_id: str, *, parent_task_id: str | None = None) -> TaskIntent:
    return TaskIntent(
        task_id=task_id,
        objective=f"执行 {task_id}",
        acceptance_criteria=("返回结果",),
        required_capabilities=("analysis",),
        constraints=("只读",),
        parent_task_id=parent_task_id,
    )


def selection(model_id: str = "pixel/gpt-5.6-luna") -> AgentSelection:
    return AgentSelection(
        provider_id="codex",
        model_id=model_id,
        effort="medium",
        rationale="测试选择",
        capability_ids=("analysis",),
    )


def decision(
    kind: CoordinatorDecisionKind,
    *,
    decision_id: str,
    tasks: tuple[TaskIntent, ...] = (),
    selected: AgentSelection | None = None,
    target_node_id: str | None = None,
    message: str | None = None,
) -> CoordinatorDecision:
    return CoordinatorDecision(
        decision_id=decision_id,
        kind=kind,
        rationale="测试决策",
        tasks=tasks,
        selection=selected,
        target_node_id=target_node_id,
        message=message,
    )


def snapshot(
    delegation_id: str,
    *,
    status: DelegationStatus = DelegationStatus.ADMITTED,
    revision: int = 1,
) -> DelegationSnapshot:
    return DelegationSnapshot(
        delegation_id=delegation_id,
        status=status,
        revision=revision,
        session_id=f"session-{delegation_id}",
        channel_id=f"channel:{delegation_id}",
        parent_delegation_id=None,
        depth=0,
        current_invocation_id=f"invocation-{delegation_id}",
        current_activation_id=f"activation-{delegation_id}",
        activation_count=1,
        child_delegation_ids=(),
        report=None,
    )


class FakeDecisionAgent:
    def __init__(
        self,
        decisions: list[CoordinatorDecision],
        *,
        max_steps: int = 8,
        expected_activation_id: str = "activation-1",
    ) -> None:
        self.config = CoordinatorAgentConfig(
            model="pixel/gpt-5.6-luna",
            api_key="test-token",
            reasoning_effort=CoordinatorReasoningEffort.MEDIUM,
            max_decision_steps=max_steps,
        )
        self._decisions = decisions
        self._expected_activation_id = expected_activation_id
        self.prompts: list[str] = []

    async def decide(
        self,
        prompt: str,
        *,
        session: AgentSession,
        activation_id: str,
        step: int,
    ) -> CoordinatorDecisionResult:
        self.prompts.append(prompt)
        assert session.session_id == "maf-session-1"
        assert activation_id == self._expected_activation_id
        assert step >= 1
        if not self._decisions:
            raise AssertionError("fake decision queue is empty")
        return CoordinatorDecisionResult(
            decision=self._decisions.pop(0),
            response_id=f"response-{step}",
            finish_reason="stop",
        )


class FakeExecutionGateway:
    def __init__(self, snapshots: list[DelegationSnapshot]) -> None:
        self.snapshots = snapshots
        self.requests: list[DelegationRequest] = []
        self.wait_calls: list[tuple[str, int]] = []
        self.message_requests: list[DelegationMessageRequest] = []
        self.cancel_requests: list[DelegationCancelRequest] = []
        self.reconciliation_requests: list[DelegationReconciliationRequest] = []

    async def delegate(self, request: DelegationRequest) -> DelegationSnapshot:
        self.requests.append(request)
        if not self.snapshots:
            raise AssertionError("fake snapshot queue is empty")
        return self.snapshots.pop(0)

    async def wait(self, delegation_id: str, *, timeout_ms: int) -> DelegationSnapshot:
        self.wait_calls.append((delegation_id, timeout_ms))
        if not self.snapshots:
            raise AssertionError("fake snapshot queue is empty")
        return self.snapshots.pop(0)

    async def send_message(self, request: DelegationMessageRequest) -> MessageDispatchSnapshot:
        self.message_requests.append(request)
        return MessageDispatchSnapshot(
            dispatch_id="dispatch-1",
            delegation_id=request.delegation_id,
            session_id=request.session_id,
            status="applied",
            revision=1,
            applied_strategy=request.delivery.value,
            previous_activation_id=request.expected_activation_id,
            current_activation_id="activation-continued",
            error_code=None,
            error_message=None,
        )

    async def cancel(self, request: DelegationCancelRequest) -> DelegationSnapshot:
        self.cancel_requests.append(request)
        if not self.snapshots:
            raise AssertionError("fake snapshot queue is empty")
        return self.snapshots.pop(0)

    async def resolve_reconciliation(
        self,
        request: DelegationReconciliationRequest,
    ) -> DelegationSnapshot:
        self.reconciliation_requests.append(request)
        if not self.snapshots:
            raise AssertionError("fake snapshot queue is empty")
        return self.snapshots.pop(0)


def make_orchestrator(
    decisions: list[CoordinatorDecision],
    snapshots: list[DelegationSnapshot],
    *,
    max_steps: int = 8,
    wait_timeout_ms: int = 25,
) -> tuple[CoordinatorOrchestrator, FakeDecisionAgent, FakeExecutionGateway]:
    agent = FakeDecisionAgent(decisions, max_steps=max_steps)
    execution = FakeExecutionGateway(snapshots)
    orchestrator = CoordinatorOrchestrator(
        agent=agent,
        execution=execution,
        config=CoordinatorOrchestratorConfig(
            workspace_root="D:/workspace",
            wait_timeout_ms=wait_timeout_ms,
        ),
    )
    return orchestrator, agent, execution


def test_orchestrator_creates_graph_and_dispatches_independent_tasks() -> None:
    orchestrator, agent, execution = make_orchestrator(
        [
            decision(
                CoordinatorDecisionKind.CREATE_PLAN,
                decision_id="create-1",
                tasks=(task("task-a"), task("task-b")),
            ),
            decision(
                CoordinatorDecisionKind.DELEGATE,
                decision_id="delegate-a",
                tasks=(task("task-a"),),
                selected=selection(),
            ),
            decision(
                CoordinatorDecisionKind.DELEGATE,
                decision_id="delegate-b",
                tasks=(task("task-b"),),
                selected=selection("deepseek/model"),
            ),
            decision(
                CoordinatorDecisionKind.RESPOND,
                decision_id="respond-1",
                message="两个任务已经启动",
            ),
        ],
        [snapshot("delegation-a"), snapshot("delegation-b")],
    )
    agent_session = AgentSession(session_id="maf-session-1")

    result = asyncio.run(
        orchestrator.activate(
            "执行两个独立分析",
            session=make_session(),
            agent_session=agent_session,
            activation_id="activation-1",
            at=at(10),
        )
    )

    assert result.outcome is CoordinatorActivationOutcome.RESPONDED
    assert result.step_count == 4
    assert result.message == "两个任务已经启动"
    assert len(result.delegations) == 2
    assert result.session.plan is not None
    assert result.session.plan_graph is not None
    assert tuple(node.status for node in result.session.plan.nodes) == (
        PlanNodeStatus.AWAITING_EVENT,
        PlanNodeStatus.AWAITING_EVENT,
    )
    assert len(execution.requests) == 2
    assert execution.requests[0].cwd == "D:/workspace"
    assert execution.requests[1].selection.model == "deepseek/model"
    assert len(agent.prompts) == 4
    assert '"delegations":[]' in agent.prompts[0]
    assert '"delegations":[{' in agent.prompts[2]


def test_orchestrator_blocks_child_until_parent_is_accepted() -> None:
    orchestrator, _agent, execution = make_orchestrator(
        [
            decision(
                CoordinatorDecisionKind.CREATE_PLAN,
                decision_id="create-1",
                tasks=(task("parent"), task("child", parent_task_id="parent")),
            ),
            decision(
                CoordinatorDecisionKind.DELEGATE,
                decision_id="delegate-child",
                tasks=(task("child", parent_task_id="parent"),),
                selected=selection(),
            ),
        ],
        [snapshot("delegation-child")],
    )

    with pytest.raises(CoordinatorPlanApplicationError, match="blocked"):
        asyncio.run(
            orchestrator.activate(
                "先执行子任务",
                session=make_session(),
                agent_session=AgentSession(session_id="maf-session-1"),
                activation_id="activation-1",
                at=at(10),
            )
        )
    assert execution.requests == []


def test_orchestrator_wait_updates_completed_node_to_review() -> None:
    orchestrator, _agent, execution = make_orchestrator(
        [
            decision(
                CoordinatorDecisionKind.CREATE_PLAN,
                decision_id="create-1",
                tasks=(task("task-a"),),
                selected=selection(),
            ),
            decision(
                CoordinatorDecisionKind.DELEGATE,
                decision_id="delegate-a",
                tasks=(task("task-a"),),
                selected=selection(),
            ),
            decision(
                CoordinatorDecisionKind.WAIT,
                decision_id="wait-a",
                target_node_id="task-a",
            ),
        ],
        [
            snapshot("delegation-a"),
            snapshot("delegation-a", status=DelegationStatus.COMPLETED, revision=2),
        ],
    )

    result = asyncio.run(
        orchestrator.activate(
            "等待结果",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
        )
    )

    assert result.outcome is CoordinatorActivationOutcome.REVIEW_REQUIRED
    assert result.session.plan is not None
    assert result.session.plan.nodes[0].status is PlanNodeStatus.REVIEW_REQUIRED
    assert execution.wait_calls == [("delegation-a", 25)]


def test_orchestrator_continues_existing_worker_session_without_redelegating() -> None:
    orchestrator, _agent, execution = make_orchestrator(
        [
            decision(
                CoordinatorDecisionKind.CREATE_PLAN,
                decision_id="create-1",
                tasks=(task("task-a"),),
            ),
            decision(
                CoordinatorDecisionKind.DELEGATE,
                decision_id="delegate-a",
                tasks=(task("task-a"),),
                selected=selection(),
            ),
            decision(
                CoordinatorDecisionKind.RESPOND,
                decision_id="respond-1",
                message="已启动",
            ),
        ],
        [snapshot("delegation-a")],
    )
    started = asyncio.run(
        orchestrator.activate(
            "启动任务",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
        )
    )

    continued = asyncio.run(
        orchestrator.continue_node(
            session=started.session,
            node_id="task-a",
            message="补充约束后继续",
            at=at(11),
        )
    )

    assert len(execution.requests) == 1
    assert len(execution.message_requests) == 1
    request = execution.message_requests[0]
    assert request.delivery.value == "interrupt_continue"
    assert request.session_id == "session-delegation-a"
    assert request.expected_activation_id == "activation-delegation-a"
    assert continued.dispatch.current_activation_id == "activation-continued"


def test_orchestrator_cancels_node_and_maps_terminal_snapshot() -> None:
    orchestrator, _agent, execution = make_orchestrator(
        [
            decision(
                CoordinatorDecisionKind.CREATE_PLAN,
                decision_id="create-1",
                tasks=(task("task-a"),),
            ),
            decision(
                CoordinatorDecisionKind.DELEGATE,
                decision_id="delegate-a",
                tasks=(task("task-a"),),
                selected=selection(),
            ),
            decision(
                CoordinatorDecisionKind.RESPOND,
                decision_id="respond-1",
                message="已启动",
            ),
        ],
        [
            snapshot("delegation-a"),
            snapshot("delegation-a", status=DelegationStatus.CANCELLED, revision=2),
        ],
    )
    started = asyncio.run(
        orchestrator.activate(
            "启动任务",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
        )
    )

    cancelled = asyncio.run(
        orchestrator.cancel_node(
            session=started.session,
            node_id="task-a",
            reason="用户取消",
            at=at(11),
        )
    )

    assert cancelled.snapshot.status is DelegationStatus.CANCELLED
    assert cancelled.session.plan is not None
    assert cancelled.session.plan.nodes[0].status is PlanNodeStatus.CANCELLED
    assert execution.cancel_requests[0].session_id == "session-delegation-a"


def test_orchestrator_reconciles_node_without_creating_a_new_delegation() -> None:
    orchestrator, _agent, execution = make_orchestrator(
        [
            decision(
                CoordinatorDecisionKind.CREATE_PLAN,
                decision_id="create-1",
                tasks=(task("task-a"),),
            ),
            decision(
                CoordinatorDecisionKind.DELEGATE,
                decision_id="delegate-a",
                tasks=(task("task-a"),),
                selected=selection(),
            ),
            decision(
                CoordinatorDecisionKind.RESPOND,
                decision_id="respond-1",
                message="已启动",
            ),
        ],
        [
            snapshot("delegation-a"),
            snapshot("delegation-a", status=DelegationStatus.COMPLETED, revision=3),
        ],
    )
    started = asyncio.run(
        orchestrator.activate(
            "启动任务",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
        )
    )

    with pytest.raises(CoordinatorPolicyApprovalRequired) as captured:
        asyncio.run(
            orchestrator.reconcile_node(
                session=started.session,
                node_id="task-a",
                expected_revision=3,
                status=ReconciliationStatus.COMPLETED,
                reason="外部会话已核验",
                output={"answer": "ok"},
                at=at(11),
            )
        )
    approval = captured.value.approval
    autonomy = captured.value.session.autonomy.resolve_approval(
        approval.approval_id,
        approved=True,
        resolved_by="reviewer-1",
        reason="已完成人工核验",
        at=at(12),
    )
    approved_session = captured.value.session.update_autonomy(autonomy, at=at(12))
    reconciled = asyncio.run(
        orchestrator.reconcile_node(
            session=approved_session,
            node_id="task-a",
            expected_revision=3,
            status=ReconciliationStatus.COMPLETED,
            reason="外部会话已核验",
            output={"answer": "ok"},
            at=at(13),
        )
    )

    assert reconciled.snapshot.status is DelegationStatus.COMPLETED
    assert reconciled.session.plan is not None
    assert reconciled.session.plan.nodes[0].status is PlanNodeStatus.REVIEW_REQUIRED
    assert len(execution.requests) == 1
    assert execution.reconciliation_requests[0].delegation_id == "delegation-a"
    assert execution.reconciliation_requests[0].output == {"answer": "ok"}


def test_orchestrator_returns_input_and_enforces_config_bounds() -> None:
    orchestrator, _agent, _execution = make_orchestrator(
        [
            decision(
                CoordinatorDecisionKind.REQUEST_INPUT,
                decision_id="input-1",
                message="请确认是否继续",
            )
        ],
        [],
    )
    result = asyncio.run(
        orchestrator.activate(
            "需要确认",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
        )
    )
    assert result.outcome is CoordinatorActivationOutcome.INPUT_REQUIRED
    assert result.message == "请确认是否继续"

    with pytest.raises(CoordinatorOrchestrationError, match="wait_timeout_ms"):
        CoordinatorOrchestratorConfig(workspace_root="D:/workspace", wait_timeout_ms=-1)


def test_orchestrator_reports_step_limit_without_extra_model_call() -> None:
    repeated = [
        decision(
            CoordinatorDecisionKind.CREATE_PLAN,
            decision_id="create-1",
            tasks=(task("task-a"),),
        ),
        decision(
            CoordinatorDecisionKind.CREATE_PLAN,
            decision_id="create-1",
            tasks=(task("task-a"),),
        ),
    ]
    orchestrator, agent, _execution = make_orchestrator(repeated, [], max_steps=2)
    result = asyncio.run(
        orchestrator.activate(
            "重复规划",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
        )
    )
    assert result.outcome is CoordinatorActivationOutcome.LIMIT_REACHED
    assert result.step_count == 2
    assert len(agent.prompts) == 2


def test_orchestrator_dispatches_all_ready_nodes_in_one_activation() -> None:
    create = decision(
        CoordinatorDecisionKind.CREATE_PLAN,
        decision_id="create-many",
        tasks=(task("task-a"), task("task-b")),
        selected=selection(),
    )
    dispatch = decision(
        CoordinatorDecisionKind.DISPATCH_READY,
        decision_id="dispatch-many",
    )
    response = decision(
        CoordinatorDecisionKind.RESPOND,
        decision_id="respond-many",
        message="两个任务都已启动",
    )
    orchestrator, _agent, execution = make_orchestrator(
        [create, dispatch, response],
        [snapshot("delegation-a"), snapshot("delegation-b")],
    )

    result = asyncio.run(
        orchestrator.activate(
            "并行启动",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
            cwd="D:/workspace",
        )
    )

    assert result.outcome is CoordinatorActivationOutcome.RESPONDED
    assert len(execution.requests) == 2
    assert {request.session_id for request in execution.requests} == {
        "coordinator-session-1:task-a",
        "coordinator-session-1:task-b",
    }
    assert result.session.autonomy.delegation_count == 2


def test_orchestrator_revises_only_unexecuted_branch_and_keeps_history() -> None:
    initial = decision(
        CoordinatorDecisionKind.CREATE_PLAN,
        decision_id="create-revision",
        tasks=(task("task-a"),),
        selected=selection(),
    )
    delegate = decision(
        CoordinatorDecisionKind.DELEGATE,
        decision_id="delegate-revision",
        tasks=(task("task-a"),),
        selected=selection(),
    )
    response = decision(
        CoordinatorDecisionKind.RESPOND,
        decision_id="respond-revision-start",
        message="已启动",
    )
    orchestrator, _agent, execution = make_orchestrator(
        [initial, delegate, response], [snapshot("delegation-a")]
    )
    started = asyncio.run(
        orchestrator.activate(
            "先执行",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
        )
    )
    revised_task = task("task-b")
    revise = decision(
        CoordinatorDecisionKind.REVISE_PLAN,
        decision_id="revise-1",
        tasks=(task("task-a"), revised_task),
        selected=selection(),
    )
    response_after_revision = decision(
        CoordinatorDecisionKind.RESPOND,
        decision_id="respond-revision",
        message="计划已修订",
    )
    revision_agent = FakeDecisionAgent(
        [revise, response_after_revision],
        expected_activation_id="activation-2",
    )
    revision_orchestrator = CoordinatorOrchestrator(
        agent=revision_agent,
        execution=execution,
        config=CoordinatorOrchestratorConfig(
            workspace_root="D:/workspace",
            wait_timeout_ms=25,
        ),
    )
    revised = asyncio.run(
        revision_orchestrator.activate(
            "增加汇总分支",
            session=started.session,
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-2",
            at=at(11),
        )
    )

    assert revised.outcome is CoordinatorActivationOutcome.RESPONDED
    assert revised.session.plan is not None
    assert revised.session.plan_revisions[-1].revision == 2
    assert revised.session.plan_revisions[-1].supersedes_revision == 1
    assert {node.intent.task_id for node in revised.session.plan.nodes} == {
        "task-a",
        "task-b",
    }
    preserved = next(node for node in revised.session.plan.nodes if node.intent.task_id == "task-a")
    assert preserved.execution is not None
    assert preserved.execution.delegation_id == "delegation-a"
    assert len(execution.requests) == 1


def test_orchestrator_rejects_changes_to_executed_task_during_revision() -> None:
    initial = decision(
        CoordinatorDecisionKind.CREATE_PLAN,
        decision_id="create-immutable",
        tasks=(task("task-a"),),
        selected=selection(),
    )
    delegate = decision(
        CoordinatorDecisionKind.DELEGATE,
        decision_id="delegate-immutable",
        tasks=(task("task-a"),),
        selected=selection(),
    )
    start_response = decision(
        CoordinatorDecisionKind.RESPOND,
        decision_id="respond-immutable",
        message="已启动",
    )
    orchestrator, _agent, execution = make_orchestrator(
        [initial, delegate, start_response], [snapshot("delegation-a")]
    )
    started = asyncio.run(
        orchestrator.activate(
            "启动",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
        )
    )
    changed_task = TaskIntent(
        task_id="task-a",
        objective="修改后的执行目标",
    )
    revision_agent = FakeDecisionAgent(
        [
            decision(
                CoordinatorDecisionKind.REVISE_PLAN,
                decision_id="revise-immutable",
                tasks=(changed_task,),
                selected=selection(),
            )
        ],
        expected_activation_id="activation-2",
    )
    revision_orchestrator = CoordinatorOrchestrator(
        agent=revision_agent,
        execution=execution,
        config=CoordinatorOrchestratorConfig(workspace_root="D:/workspace"),
    )

    with pytest.raises(
        CoordinatorPlanApplicationError,
        match="executed task task-a cannot be changed",
    ):
        asyncio.run(
            revision_orchestrator.activate(
                "非法修改",
                session=started.session,
                agent_session=AgentSession(session_id="maf-session-1"),
                activation_id="activation-2",
                at=at(11),
            )
        )


def test_orchestrator_accepts_result_then_completes_goal() -> None:
    create = decision(
        CoordinatorDecisionKind.CREATE_PLAN,
        decision_id="create-complete",
        tasks=(task("task-a"),),
        selected=selection(),
    )
    delegate = decision(
        CoordinatorDecisionKind.DELEGATE,
        decision_id="delegate-complete",
        tasks=(task("task-a"),),
        selected=selection(),
    )
    start_response = decision(
        CoordinatorDecisionKind.RESPOND,
        decision_id="respond-complete-start",
        message="已启动",
    )
    orchestrator, _agent, _execution = make_orchestrator(
        [create, delegate, start_response], [snapshot("delegation-a")]
    )
    started = asyncio.run(
        orchestrator.activate(
            "启动",
            session=make_session(),
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-1",
            at=at(10),
        )
    )
    review = started.session
    assert review.plan is not None
    plan = review.plan
    review_node = plan.nodes[0].request_review(at=at(11))
    review_plan = plan.replace_node(review_node, at=at(11)).review(at=at(11))
    review = review.attach_plan(review_plan, at=at(11))
    accept_agent = FakeDecisionAgent(
        [
            decision(
                CoordinatorDecisionKind.ACCEPT_RESULT,
                decision_id="accept-complete",
                target_node_id="task-a",
            ),
            decision(
                CoordinatorDecisionKind.COMPLETE_GOAL,
                decision_id="finish-complete",
                message="目标完成",
            ),
        ],
        expected_activation_id="activation-2",
    )
    accept_orchestrator = CoordinatorOrchestrator(
        agent=accept_agent,
        execution=_execution,
        config=CoordinatorOrchestratorConfig(workspace_root="D:/workspace"),
    )
    completed = asyncio.run(
        accept_orchestrator.activate(
            "验收结果并收口",
            session=review,
            agent_session=AgentSession(session_id="maf-session-1"),
            activation_id="activation-2",
            at=at(12),
        )
    )

    assert completed.outcome is CoordinatorActivationOutcome.STOPPED
    assert completed.session.goal is not None
    assert completed.session.goal.status is GoalStatus.COMPLETED
    assert completed.session.plan is not None
    assert completed.session.plan.status is PlanStatus.COMPLETED
    assert completed.session.plan.nodes[0].status is PlanNodeStatus.ACCEPTED
