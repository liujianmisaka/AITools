from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from misaka_coordinator_service.application import (
    AutonomyRequirement,
    CoordinatorAutonomyPolicy,
    CoordinatorPolicyDeniedError,
)
from misaka_coordinator_service.domain import (
    AgentSelection,
    AutonomyApprovalKind,
    AutonomyApprovalStatus,
    CoordinatorAutonomyState,
    CoordinatorSession,
    ExecutionReference,
    Goal,
    GoalStatus,
    Plan,
    PlanNode,
    PlanStatus,
    TaskIntent,
    dump_session,
    load_session,
)

BASE_TIME = datetime(2026, 8, 27, 8, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


def session_without_plan() -> CoordinatorSession:
    goal = Goal(
        goal_id="goal-1",
        objective="完成自主协调",
        acceptance_criteria=(),
        constraints=(),
        status=GoalStatus.ACTIVE,
        created_at=at(0),
        updated_at=at(0),
    )
    return CoordinatorSession.create(
        session_id="coordinator-1",
        cognitive_session_id="maf-1",
        at=at(0),
    ).start_goal(goal, at=at(0))


def selection(provider_id: str = "claude") -> AgentSelection:
    return AgentSelection(
        provider_id=provider_id,
        model_id="pixel/model",
        effort="medium",
        rationale="适合当前任务",
    )


def test_approval_lifecycle_is_persisted_and_consumed_once() -> None:
    policy = CoordinatorAutonomyPolicy(max_model_activations=1)
    session = session_without_plan().update_autonomy(
        CoordinatorAutonomyState(model_activation_count=1),
        at=at(1),
    )
    requirements = policy.activation_requirements(session, at=at(2))

    blocked = policy.authorize(session, requirements, at=at(2))

    assert not blocked.allowed
    assert blocked.blocked_approval is not None
    approval = blocked.blocked_approval
    assert approval.kind is AutonomyApprovalKind.BUDGET_OVERRUN
    assert load_session(dump_session(blocked.session)) == blocked.session

    autonomy = blocked.session.autonomy.resolve_approval(
        approval.approval_id,
        approved=True,
        resolved_by="user-1",
        reason="允许额外一次激活",
        at=at(3),
    )
    approved_session = blocked.session.update_autonomy(autonomy, at=at(3))
    authorized = policy.authorize(approved_session, requirements, at=at(4))

    assert authorized.allowed
    assert authorized.approvals == (approved_session.autonomy.approvals[0],)
    recorded = policy.record_action(
        authorized.session,
        authorized.approvals,
        at=at(4),
        model_activation=True,
    )
    assert recorded.autonomy.model_activation_count == 2
    assert recorded.autonomy.approvals[0].status is AutonomyApprovalStatus.CONSUMED


def test_runtime_budget_counts_active_model_time_instead_of_session_age() -> None:
    policy = CoordinatorAutonomyPolicy(max_runtime_minutes=1)
    aged_session = session_without_plan()

    assert policy.activation_requirements(aged_session, at=at(24 * 60)) == ()

    exhausted_session = aged_session.update_autonomy(
        CoordinatorAutonomyState(active_runtime_milliseconds=60_000),
        at=at(1),
    )
    requirements = policy.activation_requirements(exhausted_session, at=at(24 * 60))

    assert len(requirements) == 1
    assert requirements[0].kind is AutonomyApprovalKind.BUDGET_OVERRUN
    assert requirements[0].action_key.startswith("active_runtime:coordinator-1:1:")
    assert "active runtime budget is exhausted" in requirements[0].reason


def test_denied_approval_cannot_be_bypassed_by_reauthorizing() -> None:
    session = session_without_plan()
    requirement = AutonomyRequirement(
        kind=AutonomyApprovalKind.WORKSPACE_WRITE,
        action_key="delegate:task-1",
        reason="需要写工作区",
    )
    blocked = CoordinatorAutonomyPolicy.authorize(session, (requirement,), at=at(1))
    assert blocked.blocked_approval is not None
    autonomy = blocked.session.autonomy.resolve_approval(
        blocked.blocked_approval.approval_id,
        approved=False,
        resolved_by="user-1",
        reason="不允许写入",
        at=at(2),
    )
    denied = blocked.session.update_autonomy(autonomy, at=at(2))

    with pytest.raises(CoordinatorPolicyDeniedError, match="was denied"):
        CoordinatorAutonomyPolicy.authorize(denied, (requirement,), at=at(3))


def test_delegation_policy_detects_scope_risk_and_all_budget_limits(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    outside_root = tmp_path / "outside"
    allowed_root.mkdir()
    outside_root.mkdir()
    parent_intent = TaskIntent(task_id="parent", objective="父任务")
    child_intent = TaskIntent(
        task_id="child",
        objective="子任务",
        required_capabilities=("workspace.write", "operation.destructive"),
        parent_task_id="parent",
    )
    parent = (
        PlanNode.propose(node_id="parent", intent=parent_intent, at=at(1))
        .select(selection("codex"), at=at(1))
        .bind_execution(ExecutionReference(delegation_id="delegation-parent"), at=at(1))
        .await_event(at=at(1))
    )
    child = PlanNode.propose(node_id="child", intent=child_intent, at=at(1)).select(
        selection("claude"), at=at(1)
    )
    plan = Plan(
        plan_id="plan-1",
        goal_id="goal-1",
        status=PlanStatus.RUNNING,
        nodes=(parent, child),
        revision=1,
        created_at=at(1),
        updated_at=at(1),
    )
    session = (
        session_without_plan()
        .attach_plan(plan, at=at(1))
        .update_autonomy(
            CoordinatorAutonomyState(delegation_count=1),
            at=at(1),
        )
    )
    policy = CoordinatorAutonomyPolicy(
        max_concurrent_delegations=1,
        max_total_delegations=1,
        max_delegation_depth=0,
        allowed_provider_ids=("codex",),
        allowed_workspace_roots=(str(allowed_root),),
    )

    requirements = policy.delegation_requirements(
        session=session,
        node=child,
        cwd=str(outside_root),
    )

    assert {requirement.kind for requirement in requirements} == {
        AutonomyApprovalKind.PROVIDER_SCOPE_EXPANSION,
        AutonomyApprovalKind.NEW_WORKSPACE_ROOT,
        AutonomyApprovalKind.WORKSPACE_WRITE,
        AutonomyApprovalKind.DESTRUCTIVE_OPERATION,
        AutonomyApprovalKind.BUDGET_OVERRUN,
    }
    assert (
        sum(requirement.kind is AutonomyApprovalKind.BUDGET_OVERRUN for requirement in requirements)
        == 3
    )
