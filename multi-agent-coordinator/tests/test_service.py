import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent_framework import AgentSession

from misaka_coordinator_service.application import (
    CoordinatorActivationRequest,
    CoordinatorAgentConfig,
    CoordinatorDecision,
    CoordinatorDecisionKind,
    CoordinatorDecisionResult,
    CoordinatorOrchestrator,
    CoordinatorOrchestratorConfig,
    CoordinatorReasoningEffort,
    CoordinatorService,
)
from misaka_coordinator_service.domain import (
    AgentSelection,
    CoordinatorSession,
    TaskIntent,
)
from misaka_coordinator_service.execution import (
    DelegationMessageRequest,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
    MessageDispatchSnapshot,
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
    record = CoordinatorSessionRecord(session, agent_session)

    saved = store.save(record, expected_version=0)
    assert saved.version == 1
    loaded = store.load("coordinator-1")
    assert loaded is not None
    assert loaded.coordinator_session == record.coordinator_session
    assert loaded.agent_session.to_dict() == record.agent_session.to_dict()
    assert store.list_session_ids() == ("coordinator-1",)

    agent_session.state["marker"] = "changed"
    changed = CoordinatorSessionRecord(session, agent_session)
    store.save(changed, expected_version=1)
    loaded = store.load("coordinator-1")
    assert loaded is not None
    assert loaded.agent_session.to_dict() == changed.agent_session.to_dict()

    with pytest.raises(SessionRecordConflictError, match="expected version"):
        store.save(record, expected_version=1)


def test_jsonl_session_store_rejects_corruption(tmp_path: Path) -> None:
    path = tmp_path / "sessions.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    store = JsonlCoordinatorSessionStore(path)

    with pytest.raises(SessionRecordCorruptedError, match="line 1"):
        store.load("coordinator-1")


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

    def __init__(self, decisions: list[CoordinatorDecision]) -> None:
        self.decisions = decisions

    async def decide(
        self,
        prompt: str,
        *,
        session: AgentSession,
        activation_id: str,
        step: int,
    ) -> CoordinatorDecisionResult:
        del prompt, session, activation_id, step
        return CoordinatorDecisionResult(
            decision=self.decisions.pop(0),
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
    service.close()
