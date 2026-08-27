import asyncio
from collections.abc import AsyncIterator
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
    TaskIntent,
)
from misaka_coordinator_service.execution import (
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
    JsonlCoordinatorSessionStore,
    SessionRecordConflictError,
    SessionRecordCorruptedError,
)

BASE_TIME = datetime(2026, 8, 27, 8, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


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


def task(task_id: str) -> TaskIntent:
    return TaskIntent(task_id=task_id, objective=f"执行 {task_id}")


def selection() -> AgentSelection:
    return AgentSelection(
        provider_id="fake",
        model_id="fake/model",
        effort="medium",
        rationale="测试",
    )


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


def snapshot() -> DelegationSnapshot:
    return DelegationSnapshot(
        delegation_id="delegation-1",
        status=DelegationStatus.ADMITTED,
        revision=1,
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
    def __init__(self) -> None:
        self.requests: list[DelegationRequest] = []

    async def delegate(self, request: DelegationRequest) -> DelegationSnapshot:
        self.requests.append(request)
        return snapshot()

    async def wait(self, delegation_id: str, *, timeout_ms: int) -> DelegationSnapshot:
        del delegation_id, timeout_ms
        return snapshot()

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

    async def cancel(self, request: object) -> DelegationSnapshot:
        del request
        return snapshot()

    async def resolve_reconciliation(self, request: object) -> DelegationSnapshot:
        del request
        return snapshot()


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
            event_bridge=CoordinatorEventBridge(source=source, snapshot_observer=orchestrator),
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
                source=second_source, snapshot_observer=second_orchestrator
            ),
            event_retry_seconds=0.01,
        )
        await second_service.start()
        await _wait_for_monitor_stop(second_service)

        restored = second_service.get("coordinator-restart")
        assert restored.pending_event_activation is None
        assert restored.coordinator_session.event_cursor_for("delegation-1").next_sequence == 2
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


def _unexpected_activation_id() -> str:
    raise AssertionError("persisted activation id was not reused")
