import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent_framework import AgentSession

from misaka_coordinator_service.application import (
    AutonomyRequirement,
    CoordinatorActivationRequest,
    CoordinatorAgentConfig,
    CoordinatorAutonomyPolicy,
    CoordinatorDecision,
    CoordinatorDecisionKind,
    CoordinatorDecisionResult,
    CoordinatorEventBridge,
    CoordinatorMonitorStatus,
    CoordinatorOrchestrator,
    CoordinatorOrchestratorConfig,
    CoordinatorReasoningEffort,
    CoordinatorService,
    CoordinatorServiceValidationError,
)
from misaka_coordinator_service.domain import (
    AgentSelection,
    AutonomyApprovalKind,
    AutonomyApprovalStatus,
    CoordinatorSession,
    ExecutionReference,
    Goal,
    GoalStatus,
    Plan,
    PlanGraph,
    PlanNode,
    PlanNodeStatus,
    PlanStatus,
    TaskIntent,
)
from misaka_coordinator_service.execution import (
    DelegationCancelRequest,
    DelegationMessageRequest,
    DelegationRequest,
    DelegationSessionEvent,
    DelegationSnapshot,
    DelegationStatus,
    MessageDispatchSnapshot,
    SessionStreamEvent,
    SessionStreamEventKind,
)
from misaka_coordinator_service.persistence import (
    CoordinatorSessionRecord,
    JsonlCoordinatorEventStore,
    JsonlCoordinatorSessionStore,
    PendingEventActivation,
    SessionRecordConflictError,
    SessionRecordCorruptedError,
)

BASE_TIME = datetime(2026, 8, 27, 8, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


def pending_activation() -> PendingEventActivation:
    return PendingEventActivation(
        delegation_id="delegation-1",
        sequence=1,
        activation_id="pending-activation-1",
        event_type="delegation_changed",
        event_id="event-1",
        external_event_id="external-1",
        source_event_kind="completed",
        source_event_status="completed",
        source_event_payload={},
    )


def test_jsonl_session_store_round_trips_and_enforces_cas(tmp_path: Path) -> None:
    store = JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl")
    session = CoordinatorSession.create(
        session_id="coordinator-1",
        cognitive_session_id="maf-1",
        at=at(0),
    )
    agent_session = AgentSession(session_id="maf-1")
    record = CoordinatorSessionRecord(
        session,
        agent_session,
        working_directory="D:/workspace",
    )

    saved = store.save(record, expected_version=0)
    assert saved.version == 1
    loaded = store.load("coordinator-1")
    assert loaded is not None
    assert loaded.coordinator_session == record.coordinator_session
    assert loaded.agent_session.to_dict() == record.agent_session.to_dict()
    assert loaded.working_directory == "D:/workspace"
    assert store.list_session_ids() == ("coordinator-1",)
    listed = store.list_records()
    assert len(listed) == 1
    assert listed[0].coordinator_session == saved.coordinator_session
    assert listed[0].agent_session.to_dict() == saved.agent_session.to_dict()

    agent_session.state["marker"] = "changed"
    changed = CoordinatorSessionRecord(
        session,
        agent_session,
        working_directory="D:/workspace",
    )
    store.save(changed, expected_version=1)
    loaded = store.load("coordinator-1")
    assert loaded is not None
    assert loaded.agent_session.to_dict() == changed.agent_session.to_dict()

    with pytest.raises(SessionRecordConflictError, match="expected version"):
        store.save(record, expected_version=1)


def test_jsonl_session_store_reuses_loaded_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl")
    session = CoordinatorSession.create(
        session_id="indexed-session",
        cognitive_session_id="indexed-maf",
        at=at(0),
    )
    record = CoordinatorSessionRecord(session, AgentSession(session_id="indexed-maf"))
    original_read_latest = store._read_latest
    read_count = 0

    def counted_read_latest() -> dict[str, CoordinatorSessionRecord]:
        nonlocal read_count
        read_count += 1
        return original_read_latest()

    monkeypatch.setattr(store, "_read_latest", counted_read_latest)

    store.save(record, expected_version=0)
    assert store.load("indexed-session") is not None
    assert store.list_session_ids() == ("indexed-session",)
    assert len(store.list_records()) == 1
    assert read_count == 1


def test_session_record_restores_legacy_schema_without_working_directory() -> None:
    session = CoordinatorSession.create(
        session_id="legacy-session",
        cognitive_session_id="legacy-maf",
        at=at(0),
    )
    record = CoordinatorSessionRecord(session, AgentSession(session_id="legacy-maf"), version=1)
    payload = record.to_dict()
    payload["schema_version"] = 1
    payload.pop("working_directory")
    payload.pop("pending_event_activation")

    restored = CoordinatorSessionRecord.from_dict(payload)

    assert restored.working_directory is None


def test_jsonl_session_store_rejects_corruption(tmp_path: Path) -> None:
    path = tmp_path / "sessions.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    store = JsonlCoordinatorSessionStore(path)

    with pytest.raises(SessionRecordCorruptedError, match="line 1"):
        store.load("coordinator-1")


def test_service_archives_lists_and_restores_inactive_sessions(tmp_path: Path) -> None:
    store = JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl")
    session = CoordinatorSession.create(
        session_id="archive-session",
        cognitive_session_id="archive-maf",
        at=at(0),
    )
    store.save(
        CoordinatorSessionRecord(
            session,
            AgentSession(session_id="archive-maf"),
            working_directory="D:/workspace",
        ),
        expected_version=0,
    )
    service = CoordinatorService(
        orchestrator=CoordinatorOrchestrator(
            agent=FakeAgent([]),
            execution=FakeExecution(),
            config=CoordinatorOrchestratorConfig(),
        ),
        store=store,
        clock=lambda: at(1),
    )

    archived = asyncio.run(service.archive_session("archive-session"))

    assert archived.archived_at == at(1)
    assert service.list_session_ids() == ()
    assert service.list_session_ids(archived=True) == ("archive-session",)
    duplicate = asyncio.run(service.archive_session("archive-session"))
    assert duplicate.revision == archived.revision

    restored = asyncio.run(service.unarchive_session("archive-session"))
    assert restored.archived_at is None
    assert service.list_session_ids() == ("archive-session",)
    assert service.list_session_ids(archived=True) == ()


def test_service_rejects_archiving_active_session(tmp_path: Path) -> None:
    store = JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl")
    session = CoordinatorSession.create(
        session_id="active-session",
        cognitive_session_id="active-maf",
        at=at(0),
    ).start_goal(
        Goal(
            goal_id="active-goal",
            objective="仍在执行",
            acceptance_criteria=(),
            constraints=(),
            status=GoalStatus.ACTIVE,
            created_at=at(0),
            updated_at=at(0),
        ),
        at=at(0),
    )
    store.save(
        CoordinatorSessionRecord(session, AgentSession(session_id="active-maf")),
        expected_version=0,
    )
    service = CoordinatorService(
        orchestrator=CoordinatorOrchestrator(
            agent=FakeAgent([]),
            execution=FakeExecution(),
            config=CoordinatorOrchestratorConfig(),
        ),
        store=store,
        clock=lambda: at(1),
    )

    with pytest.raises(CoordinatorServiceValidationError, match="active work"):
        asyncio.run(service.archive_session("active-session"))


def test_service_cancels_live_delegations_before_closing_the_session(tmp_path: Path) -> None:
    store = JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl")
    session = delegated_session()
    store.save(
        CoordinatorSessionRecord(
            session,
            AgentSession(session_id=session.cognitive_session_id),
            working_directory="D:/workspace",
            pending_event_activation=pending_activation(),
        ),
        expected_version=0,
    )
    execution = FakeExecution()
    event_store = JsonlCoordinatorEventStore(tmp_path / "events.jsonl")
    service = CoordinatorService(
        orchestrator=CoordinatorOrchestrator(
            agent=FakeAgent([]),
            execution=execution,
            config=CoordinatorOrchestratorConfig(),
        ),
        store=store,
        event_store=event_store,
        clock=lambda: at(1),
    )

    cancelled = asyncio.run(service.cancel_session(session.session_id, reason="用户取消"))

    assert cancelled.goal is not None
    assert cancelled.goal.status is GoalStatus.CANCELLED
    assert cancelled.plan is not None
    assert cancelled.plan.status is PlanStatus.CANCELLED
    assert cancelled.plan.nodes[0].status is PlanNodeStatus.CANCELLED
    assert len(execution.cancel_requests) == 1
    assert execution.cancel_requests[0].idempotency_key == (
        "cancel-session:cancel-session:task-1:attempt-1"
    )
    assert [event.event_type for event in service.list_events(session.session_id)] == [
        "delegation.cancelled",
        "session.cancelled",
    ]
    assert service.get(session.session_id).pending_event_activation is None


def test_service_keeps_goal_active_when_delegation_cancellation_fails(tmp_path: Path) -> None:
    store = JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl")
    session = delegated_session(session_id="failed-cancel-session")
    store.save(
        CoordinatorSessionRecord(
            session,
            AgentSession(session_id=session.cognitive_session_id),
        ),
        expected_version=0,
    )
    execution = FakeExecution(cancel_error=RuntimeError("cancel failed"))
    service = CoordinatorService(
        orchestrator=CoordinatorOrchestrator(
            agent=FakeAgent([]),
            execution=execution,
            config=CoordinatorOrchestratorConfig(),
        ),
        store=store,
        clock=lambda: at(1),
    )

    with pytest.raises(RuntimeError, match="cancel failed"):
        asyncio.run(service.cancel_session(session.session_id, reason="用户取消"))

    persisted = service.get(session.session_id).coordinator_session
    assert persisted.goal is not None
    assert persisted.goal.status is GoalStatus.ACTIVE
    assert persisted.plan is not None
    assert persisted.plan.status is PlanStatus.WAITING
    assert persisted.plan.nodes[0].status is PlanNodeStatus.AWAITING_EVENT


def test_service_archives_a_legacy_cancelled_session_with_nonterminal_plan(
    tmp_path: Path,
) -> None:
    store = JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl")
    current = delegated_session(session_id="legacy-cancelled")
    assert current.plan is not None
    reviewed_node = current.plan.nodes[0].request_review(at=at(1))
    reviewed_plan = current.plan.replace_node(reviewed_node, at=at(1)).review(at=at(1))
    current = current.attach_plan(reviewed_plan, at=at(1))
    assert current.goal is not None
    legacy = replace(
        current,
        goal=current.goal.transition(GoalStatus.CANCELLED, at=at(2)),
        revision=current.revision + 1,
        updated_at=at(2),
    )
    store.save(
        CoordinatorSessionRecord(
            legacy,
            AgentSession(session_id=legacy.cognitive_session_id),
            pending_event_activation=pending_activation(),
        ),
        expected_version=0,
    )
    service = CoordinatorService(
        orchestrator=CoordinatorOrchestrator(
            agent=FakeAgent([]),
            execution=FakeExecution(),
            config=CoordinatorOrchestratorConfig(),
        ),
        store=store,
        clock=lambda: at(3),
    )

    assert service.archive_blocker(service.get(legacy.session_id)) is None
    archived = asyncio.run(service.archive_session(legacy.session_id))

    assert archived.archived_at == at(3)
    assert archived.plan is not None
    assert archived.plan.status is PlanStatus.CANCELLED
    assert service.get(legacy.session_id).pending_event_activation is None


def test_service_resolves_and_persists_autonomy_approval(tmp_path: Path) -> None:
    store = JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl")
    session = CoordinatorSession.create(
        session_id="approval-session",
        cognitive_session_id="approval-maf",
        at=at(0),
    )
    requirement = AutonomyRequirement(
        kind=AutonomyApprovalKind.WORKSPACE_WRITE,
        action_key="delegate:task-1",
        reason="需要写工作区",
    )
    blocked = CoordinatorAutonomyPolicy.authorize(session, (requirement,), at=at(1))
    assert blocked.blocked_approval is not None
    saved = store.save(
        CoordinatorSessionRecord(
            blocked.session,
            AgentSession(session_id="approval-maf"),
            working_directory="D:/workspace",
        ),
        expected_version=0,
    )
    service = CoordinatorService(
        orchestrator=CoordinatorOrchestrator(
            agent=FakeAgent([]),
            execution=FakeExecution(),
            config=CoordinatorOrchestratorConfig(),
        ),
        store=store,
        clock=lambda: at(2),
    )

    resolved = asyncio.run(
        service.resolve_approval(
            session_id="approval-session",
            approval_id=blocked.blocked_approval.approval_id,
            approved=True,
            actor_id="user-1",
            reason="批准本次写入",
            expected_session_revision=saved.revision,
        )
    )

    assert resolved.approval.status is AutonomyApprovalStatus.APPROVED
    reloaded = store.load("approval-session")
    assert reloaded is not None
    assert reloaded.coordinator_session.autonomy.approvals[0] == resolved.approval
    with pytest.raises(CoordinatorServiceValidationError, match="session revision"):
        asyncio.run(
            service.resolve_approval(
                session_id="approval-session",
                approval_id=resolved.approval.approval_id,
                approved=True,
                actor_id="user-1",
                reason="重复批准",
                expected_session_revision=saved.revision,
            )
        )


def test_service_starts_next_stage_only_after_the_prior_stage_is_accepted(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        session, next_intent = reviewed_two_stage_session()
        store = JsonlCoordinatorSessionStore(tmp_path / "frontier-sessions.jsonl")
        saved = store.save(
            CoordinatorSessionRecord(
                coordinator_session=session,
                agent_session=AgentSession(session_id=session.cognitive_session_id),
                working_directory="D:/workspace",
            ),
            expected_version=0,
        )
        agent = frontier_agent(next_intent)
        execution = MatchingFakeExecution(
            wait_snapshot=snapshot(status=DelegationStatus.COMPLETED, revision=2)
        )
        service = CoordinatorService(
            orchestrator=CoordinatorOrchestrator(
                agent=agent,
                execution=execution,
                config=CoordinatorOrchestratorConfig(),
            ),
            store=store,
            event_store=JsonlCoordinatorEventStore(tmp_path / "frontier-events.jsonl"),
            activation_id_factory=lambda: "activation-ready-frontier",
            clock=lambda: at(1),
        )
        await service.start()
        await service.accept_result(
            session_id=session.session_id,
            node_id="phase-1-a",
            expected_session_revision=saved.revision,
        )
        await asyncio.sleep(0)
        assert agent.activation_ids == []

        current = service.get(session.session_id)
        await service.accept_result(
            session_id=session.session_id,
            node_id="phase-1-b",
            expected_session_revision=current.revision,
        )
        await _wait_for_node_status(
            service,
            session_id=session.session_id,
            node_id=next_intent.task_id,
            status=PlanNodeStatus.AWAITING_EVENT,
        )

        persisted = service.get(session.session_id).coordinator_session
        assert persisted.plan is not None
        assert [node.status for node in persisted.plan.nodes] == [
            PlanNodeStatus.ACCEPTED,
            PlanNodeStatus.ACCEPTED,
            PlanNodeStatus.AWAITING_EVENT,
        ]
        assert agent.activation_ids == [
            "activation-ready-frontier",
            "activation-ready-frontier",
        ]
        assert len(execution.requests) == 1
        assert execution.requests[0].session_id == "frontier-session:phase-2"
        assert [event.event_type for event in service.list_events(session.session_id)] == [
            "delegation.result.accepted",
            "delegation.result.accepted",
            "activation.started",
            "coordinator.decision",
            "coordinator.decision",
            "activation.completed",
        ]
        await service.aclose()

    asyncio.run(exercise())


def test_service_recovers_an_unstarted_ready_stage_after_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        session, next_intent = accepted_two_stage_session()
        store = JsonlCoordinatorSessionStore(tmp_path / "restart-frontier-sessions.jsonl")
        store.save(
            CoordinatorSessionRecord(
                coordinator_session=session,
                agent_session=AgentSession(session_id=session.cognitive_session_id),
                working_directory="D:/workspace",
            ),
            expected_version=0,
        )
        agent = frontier_agent(next_intent)
        execution = MatchingFakeExecution()
        service = CoordinatorService(
            orchestrator=CoordinatorOrchestrator(
                agent=agent,
                execution=execution,
                config=CoordinatorOrchestratorConfig(),
            ),
            store=store,
            event_store=JsonlCoordinatorEventStore(tmp_path / "restart-frontier-events.jsonl"),
            activation_id_factory=lambda: "activation-recovered-frontier",
            clock=lambda: at(2),
        )

        await service.start()
        await _wait_for_node_status(
            service,
            session_id=session.session_id,
            node_id=next_intent.task_id,
            status=PlanNodeStatus.AWAITING_EVENT,
        )

        assert len(execution.requests) == 1
        assert agent.activation_ids == [
            "activation-recovered-frontier",
            "activation-recovered-frontier",
        ]
        assert [event.event_type for event in service.list_events(session.session_id)] == [
            "activation.started",
            "coordinator.decision",
            "coordinator.decision",
            "activation.completed",
        ]
        await service.aclose()

    asyncio.run(exercise())


def task(task_id: str) -> TaskIntent:
    return TaskIntent(task_id=task_id, objective=f"执行 {task_id}")


def selection() -> AgentSelection:
    return AgentSelection(
        provider_id="fake",
        model_id="fake/model",
        effort="medium",
        rationale="测试",
    )


def delegated_session(
    *,
    session_id: str = "cancel-session",
    cognitive_session_id: str = "cancel-maf",
) -> CoordinatorSession:
    session = CoordinatorSession.create(
        session_id=session_id,
        cognitive_session_id=cognitive_session_id,
        at=at(0),
    ).start_goal(
        Goal(
            goal_id=f"goal:{session_id}",
            objective="执行可取消任务",
            acceptance_criteria=(),
            constraints=(),
            status=GoalStatus.ACTIVE,
            created_at=at(0),
            updated_at=at(0),
        ),
        at=at(0),
    )
    node = PlanNode.propose(node_id="task-1", intent=task("task-1"), at=at(0)).select(
        selection(),
        at=at(0),
    )
    plan = Plan.draft(plan_id=f"plan:{session_id}", goal_id=f"goal:{session_id}", at=at(0))
    plan = plan.add_node(node, at=at(0)).start(at=at(0))
    node = node.bind_execution(
        ExecutionReference(
            delegation_id="delegation-1",
            activation_id="activation-1",
            invocation_id="invocation-1",
            worker_session_id="worker-1",
        ),
        at=at(0),
    ).await_event(at=at(0))
    plan = plan.replace_node(node, at=at(0)).wait(at=at(0))
    return session.attach_plan(plan, at=at(0))


def reviewed_two_stage_session() -> tuple[CoordinatorSession, TaskIntent]:
    session_id = "frontier-session"
    session = CoordinatorSession.create(
        session_id=session_id,
        cognitive_session_id="frontier-maf",
        at=at(0),
    ).start_goal(
        Goal(
            goal_id=f"goal:{session_id}",
            objective="分阶段执行审查",
            acceptance_criteria=(),
            constraints=(),
            status=GoalStatus.ACTIVE,
            created_at=at(0),
            updated_at=at(0),
        ),
        at=at(0),
    )
    first_intent = TaskIntent(task_id="phase-1-a", objective="执行第一阶段 A")
    second_intent = TaskIntent(task_id="phase-1-b", objective="执行第一阶段 B")
    next_intent = TaskIntent(
        task_id="phase-2",
        objective="执行第二阶段",
        parent_task_id=first_intent.task_id,
    )
    first = PlanNode.propose(node_id=first_intent.task_id, intent=first_intent, at=at(0)).select(
        selection(), at=at(0)
    )
    second = PlanNode.propose(node_id=second_intent.task_id, intent=second_intent, at=at(0)).select(
        selection(), at=at(0)
    )
    next_node = PlanNode.propose(node_id=next_intent.task_id, intent=next_intent, at=at(0))
    plan = Plan.draft(
        plan_id=f"plan:{session_id}",
        goal_id=f"goal:{session_id}",
        at=at(0),
    )
    for node in (first, second, next_node):
        plan = plan.add_node(node, at=at(0))
    plan = plan.start(at=at(0))
    first = (
        first.bind_execution(
            ExecutionReference(
                delegation_id="delegation-phase-1-a",
                activation_id="activation-phase-1-a",
                invocation_id="invocation-phase-1-a",
                worker_session_id="worker-phase-1-a",
            ),
            at=at(0),
        )
        .await_event(at=at(0))
        .request_review(at=at(0))
    )
    second = (
        second.bind_execution(
            ExecutionReference(
                delegation_id="delegation-phase-1-b",
                activation_id="activation-phase-1-b",
                invocation_id="invocation-phase-1-b",
                worker_session_id="worker-phase-1-b",
            ),
            at=at(0),
        )
        .await_event(at=at(0))
        .request_review(at=at(0))
    )
    plan = plan.replace_node(first, at=at(0))
    plan = plan.replace_node(second, at=at(0)).review(at=at(0))
    graph = PlanGraph.empty(plan_id=plan.plan_id, at=at(0)).add_dependency(
        node_id=next_node.node_id,
        depends_on_node_id=first.node_id,
        at=at(0),
    )
    session = session.attach_plan(plan, at=at(0)).attach_plan_graph(graph, at=at(0))
    return session, next_intent


def accepted_two_stage_session() -> tuple[CoordinatorSession, TaskIntent]:
    session, next_intent = reviewed_two_stage_session()
    plan = session.plan
    assert plan is not None
    for node_id in ("phase-1-a", "phase-1-b"):
        node = next(candidate for candidate in plan.nodes if candidate.node_id == node_id)
        plan = plan.replace_node(node.accept(at=at(1)), at=at(1))
    plan = plan.resume(at=at(1))
    return session.attach_plan(plan, at=at(1)), next_intent


def decision(
    kind: CoordinatorDecisionKind,
    *,
    decision_id: str,
    tasks: tuple[TaskIntent, ...] = (),
    selected: AgentSelection | None = None,
    message: str | None = None,
) -> CoordinatorDecision:
    return CoordinatorDecision(
        decision_id=decision_id,
        kind=kind,
        rationale="测试",
        tasks=tasks,
        selection=selected,
        target_node_id=None,
        message=message,
    )


def frontier_agent(next_intent: TaskIntent) -> "FakeAgent":
    return FakeAgent(
        [
            CoordinatorDecision(
                decision_id="delegate-phase-2",
                kind=CoordinatorDecisionKind.DELEGATE,
                rationale="start the next accepted stage",
                tasks=(next_intent,),
                selection=selection(),
                target_node_id=next_intent.task_id,
                message=None,
            ),
            decision(
                CoordinatorDecisionKind.RESPOND,
                decision_id="respond-phase-2",
                message="第二阶段已启动",
            ),
        ]
    )


def snapshot(
    *,
    status: DelegationStatus = DelegationStatus.ADMITTED,
    revision: int = 1,
) -> DelegationSnapshot:
    return DelegationSnapshot(
        delegation_id="delegation-1",
        status=status,
        revision=revision,
        session_id="worker-1",
        channel_id="channel-1",
        parent_delegation_id=None,
        depth=0,
        current_invocation_id="invocation-1",
        current_activation_id="activation-1",
        activation_count=1,
        child_delegation_ids=(),
        report=None,
    )


class FakeAgent:
    config = CoordinatorAgentConfig(
        model="fake/model",
        api_key="test",
        reasoning_effort=CoordinatorReasoningEffort.MEDIUM,
        max_decision_steps=8,
    )

    def __init__(self, decisions: list[CoordinatorDecision | Exception]) -> None:
        self.decisions = decisions
        self.prompts: list[str] = []
        self.activation_ids: list[str] = []

    async def decide(
        self,
        prompt: str,
        *,
        session: AgentSession,
        activation_id: str,
        step: int,
    ) -> CoordinatorDecisionResult:
        del session, step
        self.prompts.append(prompt)
        self.activation_ids.append(activation_id)
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return CoordinatorDecisionResult(
            decision=item,
            response_id=None,
            finish_reason="stop",
        )


class FakeExecution:
    def __init__(
        self,
        *,
        wait_snapshot: DelegationSnapshot | None = None,
        cancel_error: Exception | None = None,
    ) -> None:
        self.requests: list[DelegationRequest] = []
        self.wait_snapshot = wait_snapshot
        self.cancel_error = cancel_error
        self.cancel_requests: list[DelegationCancelRequest] = []

    async def delegate(self, request: DelegationRequest) -> DelegationSnapshot:
        self.requests.append(request)
        return snapshot()

    async def wait(self, delegation_id: str, *, timeout_ms: int) -> DelegationSnapshot:
        del delegation_id, timeout_ms
        return self.wait_snapshot or snapshot()

    async def send_message(self, request: DelegationMessageRequest) -> MessageDispatchSnapshot:
        return MessageDispatchSnapshot(
            dispatch_id="dispatch-1",
            delegation_id=request.delegation_id,
            session_id=request.session_id,
            status="applied",
            revision=1,
            applied_strategy=request.delivery.value,
            previous_activation_id=request.expected_activation_id,
            current_activation_id="activation-2",
            error_code=None,
            error_message=None,
        )

    async def cancel(self, request: DelegationCancelRequest) -> DelegationSnapshot:
        self.cancel_requests.append(request)
        if self.cancel_error is not None:
            raise self.cancel_error
        return snapshot(status=DelegationStatus.CANCELLED, revision=2)

    async def resolve_reconciliation(self, request: object) -> DelegationSnapshot:
        del request
        return snapshot()


class MatchingFakeExecution(FakeExecution):
    async def delegate(self, request: DelegationRequest) -> DelegationSnapshot:
        self.requests.append(request)
        return replace(
            snapshot(),
            delegation_id=request.delegation_id,
            session_id=request.session_id,
            channel_id=request.channel_id,
        )


class FakeSessionEventSource:
    def __init__(self, event: DelegationSessionEvent) -> None:
        self.event = event
        self.list_calls: list[int] = []

    async def list_events(
        self,
        delegation_id: str,
        *,
        next_sequence: int = 1,
    ) -> tuple[DelegationSessionEvent, ...]:
        assert delegation_id == self.event.delegation_id
        self.list_calls.append(next_sequence)
        return (self.event,) if next_sequence <= self.event.sequence else ()

    async def stream_events(
        self,
        delegation_id: str,
        *,
        next_sequence: int = 1,
    ) -> AsyncIterator[SessionStreamEvent]:
        assert delegation_id == self.event.delegation_id
        yield SessionStreamEvent(
            kind=SessionStreamEventKind.END,
            event_id=None,
            next_sequence=next_sequence,
        )


def test_coordinator_service_persists_activation_and_uses_request_cwd(
    tmp_path: Path,
) -> None:
    agent = FakeAgent(
        [
            decision(
                CoordinatorDecisionKind.CREATE_PLAN,
                decision_id="create-1",
                tasks=(task("task-1"),),
            ),
            decision(
                CoordinatorDecisionKind.DELEGATE,
                decision_id="delegate-1",
                tasks=(task("task-1"),),
                selected=selection(),
            ),
            decision(
                CoordinatorDecisionKind.RESPOND,
                decision_id="respond-1",
                message="已启动",
            ),
        ]
    )
    execution = FakeExecution()
    orchestrator = CoordinatorOrchestrator(
        agent=agent,
        execution=execution,
        config=CoordinatorOrchestratorConfig(),
    )
    service = CoordinatorService(
        orchestrator=orchestrator,
        store=JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl"),
        activation_id_factory=lambda: "activation-coordinator-1",
        clock=lambda: at(10),
    )

    result = asyncio.run(
        service.activate(
            CoordinatorActivationRequest(
                session_id="coordinator-1",
                prompt="启动委派",
                cwd="D:/arbitrary/workspace",
            )
        )
    )

    assert result.result.message == "已启动"
    assert execution.requests[0].cwd == "D:/arbitrary/workspace"
    persisted = service.get("coordinator-1")
    assert persisted.agent_session.session_id == "maf:coordinator-1"
    assert persisted.coordinator_session.plan is not None
    assert [event.event_type for event in service.list_events("coordinator-1")] == [
        "session.created",
        "user.message",
        "activation.started",
        "coordinator.decision",
        "coordinator.decision",
        "coordinator.decision",
        "activation.completed",
    ]
    service.close()


def test_coordinator_service_hides_new_session_until_initial_activation_returns(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first_decision_started = asyncio.Event()
        release_first_decision = asyncio.Event()

        class BlockingAgent(FakeAgent):
            async def decide(
                self,
                prompt: str,
                *,
                session: AgentSession,
                activation_id: str,
                step: int,
            ) -> CoordinatorDecisionResult:
                if not first_decision_started.is_set():
                    first_decision_started.set()
                    await release_first_decision.wait()
                return await super().decide(
                    prompt,
                    session=session,
                    activation_id=activation_id,
                    step=step,
                )

        agent = BlockingAgent(
            [
                decision(
                    CoordinatorDecisionKind.CREATE_PLAN,
                    decision_id="create-1",
                    tasks=(task("task-1"),),
                ),
                decision(
                    CoordinatorDecisionKind.DELEGATE,
                    decision_id="delegate-1",
                    tasks=(task("task-1"),),
                    selected=selection(),
                ),
                decision(
                    CoordinatorDecisionKind.RESPOND,
                    decision_id="respond-1",
                    message="已启动",
                ),
            ]
        )
        service = CoordinatorService(
            orchestrator=CoordinatorOrchestrator(
                agent=agent,
                execution=FakeExecution(),
                config=CoordinatorOrchestratorConfig(),
            ),
            store=JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl"),
            clock=lambda: at(10),
        )

        activation_task = asyncio.create_task(
            service.activate(
                CoordinatorActivationRequest(
                    session_id="coordinator-pending",
                    prompt="等待首次响应",
                    cwd="D:/workspace",
                )
            )
        )
        await first_decision_started.wait()
        assert service.list_session_ids() == ()

        release_first_decision.set()
        result = await activation_task
        assert result.result.message == "已启动"
        assert service.list_session_ids() == ("coordinator-pending",)
        await service.aclose()

    asyncio.run(exercise())


def test_coordinator_service_closes_monitor_from_terminal_snapshot(tmp_path: Path) -> None:
    async def exercise() -> None:
        agent = FakeAgent(
            [
                decision(
                    CoordinatorDecisionKind.CREATE_PLAN,
                    decision_id="create-1",
                    tasks=(task("task-1"),),
                ),
                decision(
                    CoordinatorDecisionKind.DELEGATE,
                    decision_id="delegate-1",
                    tasks=(task("task-1"),),
                    selected=selection(),
                ),
                decision(
                    CoordinatorDecisionKind.RESPOND,
                    decision_id="respond-1",
                    message="已启动",
                ),
            ]
        )
        execution = FakeExecution(
            wait_snapshot=snapshot(
                status=DelegationStatus.RECONCILIATION_REQUIRED,
                revision=2,
            )
        )
        orchestrator = CoordinatorOrchestrator(
            agent=agent,
            execution=execution,
            config=CoordinatorOrchestratorConfig(),
        )
        service = CoordinatorService(
            orchestrator=orchestrator,
            store=JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl"),
            activation_id_factory=lambda: "activation-coordinator-1",
            clock=lambda: at(10),
            event_bridge=CoordinatorEventBridge(
                source=FakeSessionEventSource(_session_event()),
                snapshot_observer=orchestrator,
                event_observer=orchestrator,
            ),
            event_retry_seconds=0.01,
        )
        await service.start()

        await service.activate(
            CoordinatorActivationRequest(
                session_id="coordinator-terminal-snapshot",
                prompt="启动委派",
                cwd="D:/arbitrary/workspace",
            )
        )

        status = await _wait_for_monitor_stop(service)
        record = service.get("coordinator-terminal-snapshot")
        assert status.running is False
        assert status.last_error is None
        assert record.coordinator_session.plan is not None
        assert (
            record.coordinator_session.plan.nodes[0].status
            is PlanNodeStatus.RECONCILIATION_REQUIRED
        )
        await service.aclose()

    asyncio.run(exercise())


def test_coordinator_service_recovers_events_and_triggers_bounded_activation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        agent = FakeAgent(
            [
                decision(
                    CoordinatorDecisionKind.CREATE_PLAN,
                    decision_id="create-1",
                    tasks=(task("task-1"),),
                ),
                decision(
                    CoordinatorDecisionKind.DELEGATE,
                    decision_id="delegate-1",
                    tasks=(task("task-1"),),
                    selected=selection(),
                ),
                decision(
                    CoordinatorDecisionKind.RESPOND,
                    decision_id="respond-1",
                    message="已启动",
                ),
                decision(
                    CoordinatorDecisionKind.RESPOND,
                    decision_id="event-respond-1",
                    message="事件已处理",
                ),
            ]
        )
        orchestrator = CoordinatorOrchestrator(
            agent=agent,
            execution=FakeExecution(),
            config=CoordinatorOrchestratorConfig(),
        )
        source = FakeSessionEventSource(_session_event())
        service = CoordinatorService(
            orchestrator=orchestrator,
            store=JsonlCoordinatorSessionStore(tmp_path / "sessions.jsonl"),
            activation_id_factory=iter(("activation-initial", "activation-event")).__next__,
            clock=lambda: at(10),
            event_bridge=CoordinatorEventBridge(
                source=source,
                snapshot_observer=orchestrator,
                event_observer=orchestrator,
            ),
            event_retry_seconds=0.01,
        )
        await service.start()

        await service.activate(
            CoordinatorActivationRequest(
                session_id="coordinator-events",
                prompt="启动并持续处理委派",
                cwd="D:/event-workspace",
            )
        )

        record = await _wait_for_cursor(service, "coordinator-events", next_sequence=2)
        assert record.working_directory == "D:/event-workspace"
        assert record.pending_event_activation is None
        assert agent.decisions == []
        assert agent.activation_ids == [
            "activation-initial",
            "activation-initial",
            "activation-initial",
            "activation-event",
        ]
        await _wait_for_monitor_stop(service)
        assert service.monitor_statuses()[0].last_error is None
        assert source.list_calls == [1, 2]
        await asyncio.sleep(0.03)
        assert source.list_calls == [1, 2]
        await service.aclose()
        assert not any(
            task.get_name().startswith("coordinator-monitor:")
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        )

    asyncio.run(exercise())


def test_coordinator_service_retries_persisted_event_activation_after_restart(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        state_path = tmp_path / "sessions.jsonl"
        first_agent = FakeAgent(
            [
                decision(
                    CoordinatorDecisionKind.CREATE_PLAN,
                    decision_id="create-1",
                    tasks=(task("task-1"),),
                ),
                decision(
                    CoordinatorDecisionKind.DELEGATE,
                    decision_id="delegate-1",
                    tasks=(task("task-1"),),
                    selected=selection(),
                ),
                decision(
                    CoordinatorDecisionKind.RESPOND,
                    decision_id="respond-1",
                    message="已启动",
                ),
                RuntimeError("event activation failed"),
            ]
        )
        first_orchestrator = CoordinatorOrchestrator(
            agent=first_agent,
            execution=FakeExecution(),
            config=CoordinatorOrchestratorConfig(),
        )
        first_service = CoordinatorService(
            orchestrator=first_orchestrator,
            store=JsonlCoordinatorSessionStore(state_path),
            activation_id_factory=iter(("activation-initial", "activation-event")).__next__,
            clock=lambda: at(10),
            event_bridge=CoordinatorEventBridge(
                source=FakeSessionEventSource(_session_event()),
                snapshot_observer=first_orchestrator,
                event_observer=first_orchestrator,
            ),
            event_retry_seconds=60,
        )
        await first_service.start()
        await first_service.activate(
            CoordinatorActivationRequest(
                session_id="coordinator-restart",
                prompt="启动并持续处理委派",
                cwd="D:/event-workspace",
            )
        )
        failed = await _wait_for_monitor_error(first_service)
        pending = first_service.get("coordinator-restart").pending_event_activation
        assert failed.last_error == "event activation failed"
        assert pending is not None
        assert pending.activation_id == "activation-event"
        assert pending.source_event_payload == {"answer": "done"}
        await first_service.aclose()

        second_agent = FakeAgent(
            [
                decision(
                    CoordinatorDecisionKind.RESPOND,
                    decision_id="event-retry",
                    message="已恢复",
                )
            ]
        )
        second_orchestrator = CoordinatorOrchestrator(
            agent=second_agent,
            execution=FakeExecution(),
            config=CoordinatorOrchestratorConfig(),
        )
        second_source = FakeSessionEventSource(_session_event())
        second_service = CoordinatorService(
            orchestrator=second_orchestrator,
            store=JsonlCoordinatorSessionStore(state_path),
            activation_id_factory=_unexpected_activation_id,
            clock=lambda: at(11),
            event_bridge=CoordinatorEventBridge(
                source=second_source,
                snapshot_observer=second_orchestrator,
                event_observer=second_orchestrator,
            ),
            event_retry_seconds=0.01,
        )
        await second_service.start()
        await _wait_for_monitor_stop(second_service)

        restored = second_service.get("coordinator-restart")
        assert restored.pending_event_activation is None
        assert restored.coordinator_session.event_cursor_for("delegation-1").next_sequence == 2
        assert restored.coordinator_session.plan is not None
        assert restored.coordinator_session.plan.nodes[0].status is PlanNodeStatus.REVIEW_REQUIRED
        assert second_agent.activation_ids == ["activation-event"]
        assert second_source.list_calls == [2]
        await second_service.aclose()

    asyncio.run(exercise())


def test_coordinator_service_does_not_consume_events_without_a_persisted_cwd(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        seed_agent = FakeAgent(
            [
                decision(
                    CoordinatorDecisionKind.CREATE_PLAN,
                    decision_id="create-1",
                    tasks=(task("task-1"),),
                ),
                decision(
                    CoordinatorDecisionKind.DELEGATE,
                    decision_id="delegate-1",
                    tasks=(task("task-1"),),
                    selected=selection(),
                ),
                decision(
                    CoordinatorDecisionKind.RESPOND,
                    decision_id="respond-1",
                    message="已启动",
                ),
            ]
        )
        seed_orchestrator = CoordinatorOrchestrator(
            agent=seed_agent,
            execution=FakeExecution(),
            config=CoordinatorOrchestratorConfig(),
        )
        seed_service = CoordinatorService(
            orchestrator=seed_orchestrator,
            store=JsonlCoordinatorSessionStore(tmp_path / "seed.jsonl"),
            activation_id_factory=lambda: "activation-initial",
            clock=lambda: at(10),
        )
        await seed_service.activate(
            CoordinatorActivationRequest(
                session_id="coordinator-no-cwd",
                prompt="启动委派",
                cwd="D:/temporary-workspace",
            )
        )
        seeded = seed_service.get("coordinator-no-cwd")
        seed_service.close()

        state_path = tmp_path / "sessions.jsonl"
        JsonlCoordinatorSessionStore(state_path).save(
            CoordinatorSessionRecord(
                coordinator_session=seeded.coordinator_session,
                agent_session=seeded.agent_session,
            ),
            expected_version=0,
        )
        source = FakeSessionEventSource(_session_event())
        orchestrator = CoordinatorOrchestrator(
            agent=FakeAgent([]),
            execution=FakeExecution(),
            config=CoordinatorOrchestratorConfig(),
        )
        service = CoordinatorService(
            orchestrator=orchestrator,
            store=JsonlCoordinatorSessionStore(state_path),
            event_bridge=CoordinatorEventBridge(source=source, snapshot_observer=orchestrator),
            event_retry_seconds=60,
        )
        await service.start()

        status = await _wait_for_monitor_error(service)
        assert status.last_error == (
            "persisted session has no working directory; activate it once manually"
        )
        assert source.list_calls == []
        assert (
            service.get("coordinator-no-cwd")
            .coordinator_session.event_cursor_for("delegation-1")
            .next_sequence
            == 1
        )
        await service.aclose()

    asyncio.run(exercise())


def _session_event() -> DelegationSessionEvent:
    return DelegationSessionEvent(
        delegation_id="delegation-1",
        sequence=1,
        kind="output",
        invocation_id="invocation-1",
        activation_id="activation-1",
        activation_number=1,
        status="completed",
        provider_session_id="provider-session-1",
        provider_operation_id="provider-operation-1",
        payload={"answer": "done"},
        occurred_at=at(10),
    )


async def _wait_for_cursor(
    service: CoordinatorService,
    session_id: str,
    *,
    next_sequence: int,
) -> CoordinatorSessionRecord:
    for _ in range(100):
        record = service.get(session_id)
        cursor = record.coordinator_session.event_cursor_for("delegation-1")
        if cursor.next_sequence == next_sequence and record.pending_event_activation is None:
            return record
        await asyncio.sleep(0.01)
    pytest.fail("event monitor did not persist and process the delegation event")


async def _wait_for_monitor_stop(service: CoordinatorService) -> CoordinatorMonitorStatus:
    for _ in range(100):
        statuses = service.monitor_statuses()
        if statuses and not statuses[0].running:
            return statuses[0]
        await asyncio.sleep(0.01)
    pytest.fail("event monitor did not stop")


async def _wait_for_monitor_error(service: CoordinatorService) -> CoordinatorMonitorStatus:
    for _ in range(100):
        statuses = service.monitor_statuses()
        if statuses and statuses[0].last_error is not None:
            return statuses[0]
        await asyncio.sleep(0.01)
    pytest.fail("event monitor did not report its failure")


async def _wait_for_node_status(
    service: CoordinatorService,
    *,
    session_id: str,
    node_id: str,
    status: PlanNodeStatus,
) -> None:
    for _attempt in range(100):
        plan = service.get(session_id).coordinator_session.plan
        if plan is not None:
            node = next(candidate for candidate in plan.nodes if candidate.node_id == node_id)
            if node.status is status:
                return
        await asyncio.sleep(0.01)
    pytest.fail(f"node {node_id} did not reach {status}")


def _unexpected_activation_id() -> str:
    raise AssertionError("persisted activation id was not reused")
