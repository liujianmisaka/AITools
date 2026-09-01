import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from misaka_coordinator_service.domain import (
    AgentSelection,
    AutonomyApproval,
    AutonomyApprovalKind,
    CoordinatorAutonomyState,
    CoordinatorDomainError,
    CoordinatorEvent,
    CoordinatorEventType,
    CoordinatorSession,
    ExecutionEventCursor,
    ExecutionReference,
    Goal,
    GoalStatus,
    InvalidTransitionError,
    Plan,
    PlanGraph,
    PlanNode,
    PlanNodeStatus,
    PlanRevision,
    PlanStatus,
    ReviewDecision,
    ReviewDecisionKind,
    TaskIntent,
    dump_session,
    load_session,
)

BASE_TIME = datetime(2026, 8, 27, 8, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


def make_goal() -> Goal:
    return Goal(
        goal_id="goal-1",
        objective="完成跨 Agent 调研",
        acceptance_criteria=("形成有来源的结论",),
        constraints=("禁止 Push",),
        status=GoalStatus.ACTIVE,
        created_at=at(0),
        updated_at=at(0),
    )


def make_intent() -> TaskIntent:
    return TaskIntent(
        task_id="task-1",
        objective="调研 Agent SDK",
        acceptance_criteria=("覆盖 Codex 和 Claude",),
        required_capabilities=("research",),
        constraints=("只读",),
    )


def make_selection() -> AgentSelection:
    return AgentSelection(
        provider_id="codex",
        model_id="pixel/gpt-5.6-luna",
        effort="medium",
        rationale="适合代码与 SDK 调研",
        capability_ids=("research",),
    )


def make_execution() -> ExecutionReference:
    return ExecutionReference(
        delegation_id="delegation-1",
        activation_id="activation-1",
        invocation_id="invocation-1",
        worker_session_id="worker-session-1",
    )


def make_running_session() -> CoordinatorSession:
    goal = make_goal()
    session = CoordinatorSession.create(
        session_id="coordinator-session-1",
        cognitive_session_id="maf-session-1",
        at=at(0),
    ).start_goal(goal, at=at(1))
    node = PlanNode.propose(node_id="node-1", intent=make_intent(), at=at(1)).select(
        make_selection(), at=at(2)
    )
    plan = Plan.draft(plan_id="plan-1", goal_id=goal.goal_id, at=at(1))
    plan = plan.add_node(node, at=at(2)).start(at=at(3))
    return session.attach_plan(plan, at=at(3))


def test_plan_node_lifecycle_keeps_only_v3_identifiers() -> None:
    node = PlanNode.propose(node_id="node-1", intent=make_intent(), at=at(1))
    assert node.status is PlanNodeStatus.PROPOSED

    node = node.select(make_selection(), at=at(2))
    node = node.bind_execution(make_execution(), at=at(3))
    node = node.await_event(at=at(4))
    node = node.request_review(at=at(5))
    node = node.accept(at=at(6))

    assert node.status is PlanNodeStatus.ACCEPTED
    assert node.execution is not None
    assert node.execution.to_dict() == {
        "delegation_id": "delegation-1",
        "activation_id": "activation-1",
        "invocation_id": "invocation-1",
        "worker_session_id": "worker-session-1",
    }
    with pytest.raises(InvalidTransitionError):
        node.retry(at=at(7))


def test_plan_can_start_after_first_node_is_prepared() -> None:
    node = PlanNode.propose(node_id="node-1", intent=make_intent(), at=at(1)).select(
        make_selection(), at=at(2)
    )
    draft = Plan.draft(plan_id="plan-1", goal_id="goal-1", at=at(1)).add_node(node, at=at(2))

    running = draft.start(at=at(3))

    assert running.status is PlanStatus.RUNNING
    with pytest.raises(CoordinatorDomainError, match="prepared node"):
        Plan.draft(plan_id="empty", goal_id="goal-1", at=at(1)).start(at=at(2))


def test_retry_clears_execution_and_increments_attempt() -> None:
    node = PlanNode.propose(node_id="node-1", intent=make_intent(), at=at(1))
    node = node.select(make_selection(), at=at(2))
    node = node.bind_execution(make_execution(), at=at(3))
    node = node.request_review(at=at(4))

    retried = node.retry(at=at(5))

    assert retried.status is PlanNodeStatus.READY
    assert retried.execution is None
    assert retried.selection == node.selection
    assert retried.attempt == 2


def test_reconciliation_is_distinct_from_result_review() -> None:
    node = PlanNode.propose(node_id="node-1", intent=make_intent(), at=at(1))
    node = node.select(make_selection(), at=at(2))
    node = node.bind_execution(make_execution(), at=at(3)).await_event(at=at(4))

    reconciling = node.request_reconciliation(at=at(5))

    assert reconciling.status is PlanNodeStatus.RECONCILIATION_REQUIRED
    reviewed = reconciling.request_review(at=at(6))
    assert reviewed.status is PlanNodeStatus.REVIEW_REQUIRED
    with pytest.raises(InvalidTransitionError):
        reconciling.retry(at=at(6))


def test_completed_session_round_trips_without_framework_state() -> None:
    goal = make_goal()
    session = CoordinatorSession.create(
        session_id="coordinator-session-1",
        cognitive_session_id="maf-session-1",
        at=at(0),
    ).start_goal(goal, at=at(1))

    node = PlanNode.propose(node_id="node-1", intent=make_intent(), at=at(1))
    node = node.select(make_selection(), at=at(2))
    plan = Plan.draft(plan_id="plan-1", goal_id=goal.goal_id, at=at(1))
    plan = plan.add_node(node, at=at(2)).mark_ready(at=at(3)).start(at=at(4))
    session = session.attach_plan(plan, at=at(4))

    node = node.bind_execution(make_execution(), at=at(5)).await_event(at=at(6))
    plan = plan.replace_node(node, at=at(6)).wait(at=at(6))
    session = session.attach_plan(plan, at=at(6))
    event = CoordinatorEvent(
        event_id="event-1",
        session_id=session.session_id,
        event_type=CoordinatorEventType.DELEGATION_CHANGED,
        source="multi-agent-v3",
        occurred_at=at(7),
        node_id=node.node_id,
        execution=make_execution(),
        external_event_id="v3-event-42",
    )
    session = session.record_event(event, at=at(8))

    node = node.request_review(at=at(9)).accept(at=at(10))
    plan = plan.replace_node(node, at=at(10)).review(at=at(10)).complete(at=at(11))
    session = session.attach_plan(plan, at=at(11)).complete_goal(at=at(12))

    restored = load_session(dump_session(session))

    assert restored == session
    assert restored.goal is not None
    assert restored.goal.status is GoalStatus.COMPLETED
    assert restored.plan is not None
    assert restored.plan.status is PlanStatus.COMPLETED
    assert restored.last_event_id == event.event_id


def test_session_archive_round_trips_and_requires_inactive_work() -> None:
    session = CoordinatorSession.create(
        session_id="archive-session",
        cognitive_session_id="archive-maf",
        at=at(0),
    )

    archived = session.archive(at=at(1))

    assert archived.archived_at == at(1)
    assert archived.revision == session.revision + 1
    assert archived.archive(at=at(2)) is archived
    assert load_session(dump_session(archived)) == archived

    unarchived = archived.unarchive(at=at(2))
    assert unarchived.archived_at is None
    assert unarchived.revision == archived.revision + 1
    assert unarchived.unarchive(at=at(3)) is unarchived

    active = session.start_goal(make_goal(), at=at(1))
    with pytest.raises(InvalidTransitionError, match="active work"):
        active.archive(at=at(2))
    with pytest.raises(InvalidTransitionError, match="archived session"):
        archived.start_goal(make_goal(), at=at(2))


def test_terminal_goal_transitions_close_the_plan() -> None:
    running = make_running_session()

    cancelled = running.cancel_goal(at=at(4))
    failed = running.fail_goal(at=at(4))

    assert cancelled.goal is not None
    assert cancelled.goal.status is GoalStatus.CANCELLED
    assert cancelled.plan is not None
    assert cancelled.plan.status is PlanStatus.CANCELLED
    assert cancelled.can_archive is True
    assert failed.goal is not None
    assert failed.goal.status is GoalStatus.FAILED
    assert failed.plan is not None
    assert failed.plan.status is PlanStatus.FAILED
    assert failed.can_archive is True


def test_archive_closes_a_legacy_plan_for_a_cancelled_goal() -> None:
    session = make_running_session()
    assert session.plan is not None
    node = (
        session.plan.nodes[0]
        .bind_execution(make_execution(), at=at(4))
        .await_event(at=at(5))
        .request_review(at=at(6))
    )
    plan = session.plan.replace_node(node, at=at(6)).review(at=at(6))
    reviewing = session.attach_plan(plan, at=at(6))
    assert reviewing.goal is not None
    legacy = replace(
        reviewing,
        goal=reviewing.goal.transition(GoalStatus.CANCELLED, at=at(7)),
        revision=reviewing.revision + 1,
        updated_at=at(7),
    )

    assert legacy.can_archive is True
    archived = legacy.archive(at=at(8))

    assert archived.archived_at == at(8)
    assert archived.plan is not None
    assert archived.plan.status is PlanStatus.CANCELLED


def test_archive_keeps_blocking_a_legacy_goal_with_a_live_delegation() -> None:
    session = make_running_session()
    assert session.plan is not None
    node = session.plan.nodes[0].bind_execution(make_execution(), at=at(4)).await_event(at=at(5))
    plan = session.plan.replace_node(node, at=at(5)).wait(at=at(5))
    waiting = session.attach_plan(plan, at=at(5))
    with pytest.raises(InvalidTransitionError, match="live delegations"):
        waiting.cancel_goal(at=at(6))
    with pytest.raises(InvalidTransitionError, match="live delegations"):
        waiting.fail_goal(at=at(6))
    assert waiting.goal is not None
    legacy = replace(
        waiting,
        goal=waiting.goal.transition(GoalStatus.CANCELLED, at=at(6)),
        revision=waiting.revision + 1,
        updated_at=at(6),
    )

    assert legacy.has_live_delegations is True
    assert legacy.can_archive is False
    with pytest.raises(InvalidTransitionError, match="active work"):
        legacy.archive(at=at(7))


def test_session_event_cursor_is_idempotent_and_monotonic() -> None:
    session = CoordinatorSession.create(
        session_id="coordinator-session-1",
        cognitive_session_id="maf-session-1",
        at=at(0),
    )
    event = CoordinatorEvent(
        event_id="event-1",
        session_id=session.session_id,
        event_type=CoordinatorEventType.USER_MESSAGE,
        source="user",
        occurred_at=at(2),
    )
    recorded = session.record_event(event, at=at(3))

    assert recorded.record_event(event, at=at(4)) is recorded
    with pytest.raises(CoordinatorDomainError, match="move backwards"):
        recorded.record_event(
            CoordinatorEvent(
                event_id="event-2",
                session_id=session.session_id,
                event_type=CoordinatorEventType.TIMER_FIRED,
                source="temporal",
                occurred_at=at(1),
            ),
            at=at(4),
        )
    with pytest.raises(CoordinatorDomainError, match="session_id"):
        recorded.record_event(
            CoordinatorEvent(
                event_id="event-3",
                session_id="another-session",
                event_type=CoordinatorEventType.USER_MESSAGE,
                source="user",
                occurred_at=at(3),
            ),
            at=at(4),
        )


def test_execution_event_cursor_is_contiguous_and_serializable() -> None:
    cursor = ExecutionEventCursor(delegation_id="delegation-1")
    advanced = cursor.advance(1)
    assert advanced.next_sequence == 2
    assert advanced.advance(1) is advanced
    with pytest.raises(CoordinatorDomainError, match="skip"):
        cursor.advance(2)
    assert ExecutionEventCursor.from_dict(cursor.to_dict()) == cursor


def test_session_keeps_independent_execution_event_cursors() -> None:
    session = CoordinatorSession.create(
        session_id="coordinator-session-1",
        cognitive_session_id="maf-session-1",
        at=at(0),
    )
    first = session.advance_event_cursor("delegation-a", 1, at=at(1))
    second = first.advance_event_cursor("delegation-b", 1, at=at(2))
    duplicate = second.advance_event_cursor("delegation-a", 1, at=at(3))

    assert duplicate is second
    assert tuple(
        (cursor.delegation_id, cursor.next_sequence) for cursor in second.event_cursors
    ) == (
        ("delegation-a", 2),
        ("delegation-b", 2),
    )
    restored = load_session(dump_session(second))
    assert restored == second


def test_session_rejects_stale_plan_revision() -> None:
    goal = make_goal()
    session = CoordinatorSession.create(
        session_id="coordinator-session-1",
        cognitive_session_id="maf-session-1",
        at=at(0),
    ).start_goal(goal, at=at(1))
    node = PlanNode.propose(node_id="node-1", intent=make_intent(), at=at(1))
    node = node.select(make_selection(), at=at(2))
    plan = Plan.draft(plan_id="plan-1", goal_id=goal.goal_id, at=at(1))
    plan = plan.add_node(node, at=at(2)).mark_ready(at=at(3))
    session = session.attach_plan(plan, at=at(3))
    advanced = plan.start(at=at(4))
    session = session.attach_plan(advanced, at=at(4))

    with pytest.raises(CoordinatorDomainError, match="revision"):
        session.attach_plan(plan.cancel(at=at(5)), at=at(5))


def test_review_decision_requires_follow_up_for_retry() -> None:
    with pytest.raises(CoordinatorDomainError, match="follow_up_prompt"):
        ReviewDecision(
            decision_id="decision-1",
            plan_id="plan-1",
            node_id="node-1",
            kind=ReviewDecisionKind.RETRY,
            rationale="结果缺少来源",
            follow_up_prompt=None,
            created_at=at(1),
        )

    decision = ReviewDecision(
        decision_id="decision-1",
        plan_id="plan-1",
        node_id="node-1",
        kind=ReviewDecisionKind.RETRY,
        rationale="结果缺少来源",
        follow_up_prompt="补充权威来源后重新回答",
        created_at=at(1),
    )
    assert ReviewDecision.from_dict(decision.to_dict()) == decision


def test_session_schema_and_execution_reference_are_strict() -> None:
    session = CoordinatorSession.create(
        session_id="coordinator-session-1",
        cognitive_session_id="maf-session-1",
        at=at(0),
    )
    payload = dump_session(session).replace('"schema_version":5', '"schema_version":6')

    with pytest.raises(CoordinatorDomainError, match="schema_version 6"):
        load_session(payload)
    with pytest.raises(CoordinatorDomainError, match="activation_id"):
        ExecutionReference(delegation_id="delegation-1", invocation_id="invocation-1")


def test_session_schema_version_1_restores_with_empty_autonomy_state() -> None:
    session = CoordinatorSession.create(
        session_id="legacy-coordinator-session",
        cognitive_session_id="legacy-maf-session",
        at=at(0),
    )
    payload = session.to_dict()
    payload["schema_version"] = 1
    payload.pop("autonomy")

    restored = CoordinatorSession.from_dict(payload)

    assert restored.autonomy.model_activation_count == 0
    assert restored.autonomy.delegation_count == 0
    assert restored.autonomy.plan_revision_count == 0
    assert restored.autonomy.active_runtime_milliseconds == 0
    assert restored.autonomy.approvals == ()
    assert restored.archived_at is None


def test_session_schema_version_3_restores_as_unarchived() -> None:
    payload = CoordinatorSession.create(
        session_id="schema-3-session",
        cognitive_session_id="schema-3-maf",
        at=at(0),
    ).to_dict()
    payload["schema_version"] = 3
    payload.pop("archived_at")

    restored = CoordinatorSession.from_dict(payload)

    assert restored.archived_at is None


def test_session_schema_version_4_migrates_wall_clock_runtime_approval() -> None:
    legacy_approval = AutonomyApproval.request(
        kind=AutonomyApprovalKind.BUDGET_OVERRUN,
        action_key="runtime:legacy-session:3:120",
        reason="Coordinator runtime budget has expired (120 minutes)",
        at=at(1),
    )
    session = CoordinatorSession.create(
        session_id="legacy-session",
        cognitive_session_id="legacy-maf",
        at=at(0),
    ).update_autonomy(
        CoordinatorAutonomyState(approvals=(legacy_approval,)),
        at=at(1),
    )
    payload = session.to_dict()
    payload["schema_version"] = 4
    autonomy = cast(dict[str, object], payload["autonomy"])
    autonomy.pop("active_runtime_milliseconds")

    restored = CoordinatorSession.from_dict(payload)

    assert restored.autonomy.active_runtime_milliseconds == 0
    assert restored.autonomy.approvals == ()
    assert restored.archived_at is None


def test_session_schema_version_3_requires_autonomy_state() -> None:
    payload = CoordinatorSession.create(
        session_id="coordinator-session",
        cognitive_session_id="maf-session",
        at=at(0),
    ).to_dict()
    payload["schema_version"] = 3
    payload.pop("autonomy")
    payload.pop("archived_at")

    with pytest.raises(CoordinatorDomainError, match="autonomy must be an object"):
        CoordinatorSession.from_dict(payload)


def test_plan_revision_history_round_trips_and_requires_contiguous_revisions() -> None:
    goal = make_goal()
    session = CoordinatorSession.create(
        session_id="revision-session",
        cognitive_session_id="revision-maf",
        at=at(0),
    ).start_goal(goal, at=at(1))
    node = PlanNode.propose(node_id="node-a", intent=make_intent(), at=at(1))
    plan = Plan.draft(plan_id="plan-revision", goal_id=goal.goal_id, at=at(1)).add_node(
        node, at=at(1)
    )
    graph = PlanGraph.empty(plan_id=plan.plan_id, at=at(1))
    session = session.attach_plan(plan, at=at(1)).attach_plan_graph(graph, at=at(1))
    first = PlanRevision.create(
        plan_id=plan.plan_id,
        objective=goal.objective,
        rationale="初始计划",
        tasks=(node.intent,),
        previous=None,
        at=at(1),
    )
    session = session.append_plan_revision(first, at=at(1))
    second = PlanRevision.create(
        plan_id=plan.plan_id,
        objective=goal.objective,
        rationale="增加验收",
        tasks=(node.intent,),
        previous=first,
        at=at(2),
    )
    session = session.append_plan_revision(second, at=at(2))

    restored = load_session(dump_session(session))

    assert restored.plan_revisions == (first, second)
    with pytest.raises(CoordinatorDomainError, match="plan revision must be 3"):
        session.append_plan_revision(second, at=at(3))


def test_session_persists_plan_graph_and_rejects_stale_graph() -> None:
    goal = make_goal()
    session = CoordinatorSession.create(
        session_id="coordinator-session-1",
        cognitive_session_id="maf-session-1",
        at=at(0),
    ).start_goal(goal, at=at(1))
    node_a = PlanNode.propose(node_id="node-a", intent=make_intent(), at=at(1)).select(
        make_selection(), at=at(2)
    )
    node_b = PlanNode.propose(
        node_id="node-b",
        intent=TaskIntent(task_id="task-2", objective="汇总"),
        at=at(1),
    ).select(make_selection(), at=at(2))
    plan = Plan.draft(plan_id="plan-1", goal_id=goal.goal_id, at=at(1))
    plan = plan.add_node(node_a, at=at(2)).add_node(node_b, at=at(2))
    graph = PlanGraph.empty(plan_id=plan.plan_id, at=at(2)).add_dependency(
        node_id="node-b", depends_on_node_id="node-a", at=at(3)
    )
    session = session.attach_plan(plan, at=at(3)).attach_plan_graph(graph, at=at(3))

    restored = load_session(dump_session(session))
    assert restored == session
    assert restored.plan_graph == graph

    stale = PlanGraph.empty(plan_id=plan.plan_id, at=at(4))
    with pytest.raises(CoordinatorDomainError, match="plan graph revision"):
        session.attach_plan_graph(stale, at=at(4))


def test_domain_layer_does_not_import_framework_or_v3_implementations() -> None:
    domain_root = Path(__file__).parents[1] / "src" / "misaka_coordinator_service" / "domain"
    forbidden_roots = {
        "agent_framework",
        "misaka_control_plane",
        "misaka_invocation_runtime",
        "misaka_codex_provider",
        "misaka_claude_provider",
    }

    imported_roots: set[str] = set()
    for source_path in domain_root.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots.add(node.module.split(".", maxsplit=1)[0])

    assert imported_roots.isdisjoint(forbidden_roots)
