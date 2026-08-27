import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from agent_framework import AgentSession

from misaka_coordinator_service.application import (
    CoordinatorEventBridge,
    CoordinatorEventBridgeConfig,
    CoordinatorEventRecoveryError,
    CoordinatorEventUpdate,
    CoordinatorEventUpdateKind,
)
from misaka_coordinator_service.domain import (
    AgentSelection,
    CoordinatorSession,
    ExecutionReference,
    Goal,
    GoalStatus,
    Plan,
    PlanNode,
    PlanNodeStatus,
    TaskIntent,
)
from misaka_coordinator_service.execution import (
    DelegationSessionEvent,
    DelegationSessionSnapshot,
    DelegationSnapshot,
    DelegationStatus,
    SessionStreamEvent,
    SessionStreamEventKind,
)

BASE_TIME = datetime(2026, 8, 27, 8, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


def make_session() -> CoordinatorSession:
    goal = Goal(
        goal_id="goal-1",
        objective="完成委派",
        acceptance_criteria=("获得结果",),
        constraints=(),
        status=GoalStatus.ACTIVE,
        created_at=at(0),
        updated_at=at(0),
    )
    node = PlanNode.propose(
        node_id="task-a",
        intent=TaskIntent(task_id="task-a", objective="执行任务"),
        at=at(1),
    ).select(
        AgentSelection(
            provider_id="codex",
            model_id="pixel/gpt-5.6-luna",
            effort="medium",
            rationale="测试",
        ),
        at=at(2),
    )
    plan = (
        Plan.draft(plan_id="plan-1", goal_id=goal.goal_id, at=at(1))
        .add_node(node, at=at(2))
        .mark_ready(at=at(3))
        .start(at=at(4))
    )
    node = node.bind_execution(
        ExecutionReference(
            delegation_id="delegation-a",
            activation_id="activation-a",
            invocation_id="invocation-a",
            worker_session_id="worker-a",
        ),
        at=at(5),
    ).await_event(at=at(6))
    plan = plan.replace_node(node, at=at(6))
    return (
        CoordinatorSession.create(
            session_id="coordinator-1",
            cognitive_session_id="maf-1",
            at=at(0),
        )
        .start_goal(goal, at=at(1))
        .attach_plan(plan, at=at(6))
    )


def make_event(
    *, sequence: int, kind: str = "progress", status: str = "active"
) -> DelegationSessionEvent:
    return DelegationSessionEvent(
        delegation_id="delegation-a",
        sequence=sequence,
        kind=kind,
        invocation_id="invocation-a",
        activation_id="activation-a",
        activation_number=1,
        status=status,
        provider_session_id="provider-a",
        provider_operation_id="operation-a",
        payload={"sequence": sequence},
        occurred_at=at(sequence),
    )


class FakeSource:
    def __init__(
        self,
        history: tuple[DelegationSessionEvent, ...] = (),
        streams: tuple[tuple[SessionStreamEvent, ...], ...] = (),
    ) -> None:
        self.history = history
        self.streams = list(streams)
        self.list_calls: list[tuple[str, int]] = []
        self.stream_calls: list[tuple[str, int]] = []

    async def list_events(
        self,
        delegation_id: str,
        *,
        next_sequence: int = 1,
    ) -> tuple[DelegationSessionEvent, ...]:
        self.list_calls.append((delegation_id, next_sequence))
        return tuple(event for event in self.history if event.sequence >= next_sequence)

    def stream_events(
        self,
        delegation_id: str,
        *,
        next_sequence: int = 1,
    ) -> AsyncIterator[SessionStreamEvent]:
        self.stream_calls.append((delegation_id, next_sequence))
        frames = self.streams.pop(0)

        async def generate() -> AsyncIterator[SessionStreamEvent]:
            for frame in frames:
                yield frame

        return generate()


def event_frame(event: DelegationSessionEvent) -> SessionStreamEvent:
    return SessionStreamEvent(
        kind=SessionStreamEventKind.EVENT,
        event_id=f"event-{event.sequence}",
        session_event=event,
    )


def end_frame(next_sequence: int) -> SessionStreamEvent:
    return SessionStreamEvent(
        kind=SessionStreamEventKind.END,
        event_id=None,
        next_sequence=next_sequence,
    )


def snapshot_frame() -> SessionStreamEvent:
    snapshot = DelegationSnapshot(
        delegation_id="delegation-a",
        status=DelegationStatus.COMPLETED,
        revision=2,
        session_id="worker-a",
        channel_id="channel-a",
        parent_delegation_id=None,
        depth=0,
        current_invocation_id="invocation-a",
        current_activation_id="activation-a",
        activation_count=1,
        child_delegation_ids=(),
        report=None,
    )
    return SessionStreamEvent(
        kind=SessionStreamEventKind.SNAPSHOT,
        event_id="snapshot-0",
        snapshot=DelegationSessionSnapshot(
            delegation=snapshot,
            provider_id="codex",
            model="pixel/gpt-5.6-luna",
            effort="medium",
            provider_session_id="provider-a",
            provider_operation_id="operation-a",
            activation_number=1,
            last_sequence=0,
            stage="completed",
            closed=False,
            updated_at=at(1),
        ),
    )


def test_event_bridge_replays_history_and_is_idempotent() -> None:
    source = FakeSource(
        history=(
            make_event(sequence=1),
            make_event(sequence=2, kind="output", status="completed"),
        )
    )
    bridge = CoordinatorEventBridge(source=source)

    first = asyncio.run(bridge.replay(make_session(), "delegation-a", node_id="task-a", at=at(10)))
    second = asyncio.run(bridge.replay(first.session, "delegation-a", node_id="task-a", at=at(11)))

    assert [
        update.source_event.sequence for update in first.updates if update.source_event is not None
    ] == [1, 2]
    assert first.cursor.next_sequence == 3
    assert first.session.plan is not None
    assert first.session.plan.nodes[0].status is PlanNodeStatus.AWAITING_EVENT
    assert second.updates == ()
    assert second.session is first.session
    assert source.list_calls == [("delegation-a", 1), ("delegation-a", 3)]


def test_event_bridge_rejects_sequence_gap_and_bad_end_cursor() -> None:
    source = FakeSource(history=(make_event(sequence=2),))
    bridge = CoordinatorEventBridge(source=source)

    with pytest.raises(CoordinatorEventRecoveryError, match="sequence gap"):
        asyncio.run(bridge.replay(make_session(), "delegation-a", at=at(10)))

    source = FakeSource(streams=((end_frame(2),),))
    bridge = CoordinatorEventBridge(source=source)

    async def consume() -> list[CoordinatorEventUpdate]:
        return [item async for item in bridge.consume(make_session(), "delegation-a", at=at(10))]

    with pytest.raises(CoordinatorEventRecoveryError, match="unconsumed sequence"):
        asyncio.run(consume())


def test_event_bridge_reconnects_after_disconnect_using_persisted_cursor() -> None:
    first = make_event(sequence=1)
    second = make_event(sequence=2, kind="output", status="completed")
    source = FakeSource(
        streams=((event_frame(first),), (event_frame(second), end_frame(3))),
        history=(),
    )
    bridge = CoordinatorEventBridge(
        source=source,
        config=CoordinatorEventBridgeConfig(max_reconnects=1),
    )

    async def consume() -> list[CoordinatorEventUpdate]:
        return [item async for item in bridge.consume(make_session(), "delegation-a", at=at(10))]

    updates = asyncio.run(consume())

    assert [
        update.source_event.sequence for update in updates if update.source_event is not None
    ] == [1, 2]
    assert source.stream_calls == [("delegation-a", 1), ("delegation-a", 2)]


def test_event_bridge_maps_snapshot_and_calls_snapshot_observer() -> None:
    class Observer:
        def __init__(self) -> None:
            self.snapshots: list[DelegationSnapshot] = []

        def observe_snapshot(
            self,
            *,
            session: CoordinatorSession,
            node_id: str,
            snapshot: DelegationSnapshot,
            at: datetime,
        ) -> CoordinatorSession:
            self.snapshots.append(snapshot)
            assert node_id == "task-a"
            assert at == at_time
            return session

    at_time = at(10)
    source = FakeSource(streams=((snapshot_frame(), end_frame(1)),))
    observer = Observer()
    bridge = CoordinatorEventBridge(source=source, snapshot_observer=observer)

    async def consume() -> list[CoordinatorEventUpdate]:
        return [item async for item in bridge.consume(make_session(), "delegation-a", at=at_time)]

    updates = asyncio.run(consume())

    assert [update.kind for update in updates] == [
        CoordinatorEventUpdateKind.SNAPSHOT,
        CoordinatorEventUpdateKind.END,
    ]
    assert observer.snapshots[0].status is DelegationStatus.COMPLETED


def test_event_bridge_marks_terminal_events_for_activation() -> None:
    source = FakeSource(history=(make_event(sequence=1, kind="output", status="completed"),))
    bridge = CoordinatorEventBridge(source=source)
    result = asyncio.run(bridge.replay(make_session(), "delegation-a", at=at(10)))

    update = result.updates[0]
    assert update.activation_required is True
    assert update.coordinator_event is not None
    assert update.coordinator_event.event_type.value == "output_available"


def test_event_bridge_marks_agent_questions_for_activation() -> None:
    source = FakeSource(
        history=(make_event(sequence=1, kind="agent_question", status="waiting_input"),)
    )
    bridge = CoordinatorEventBridge(source=source)

    result = asyncio.run(bridge.replay(make_session(), "delegation-a", at=at(10)))

    update = result.updates[0]
    assert update.activation_required is True
    assert update.coordinator_event is not None
    assert update.coordinator_event.event_type.value == "delegation_changed"


def test_event_bridge_can_trigger_a_new_bounded_activation() -> None:
    source = FakeSource(history=(make_event(sequence=1, kind="output", status="completed"),))
    bridge = CoordinatorEventBridge(source=source)
    result = asyncio.run(bridge.replay(make_session(), "delegation-a", at=at(10)))
    update = result.updates[0]

    class FakeOrchestrator:
        def __init__(self) -> None:
            self.prompt: str | None = None

        async def activate(self, prompt: str, **kwargs: object) -> str:
            self.prompt = prompt
            assert isinstance(kwargs["session"], CoordinatorSession)
            assert isinstance(kwargs["agent_session"], AgentSession)
            return "activation-result"

    orchestrator = FakeOrchestrator()
    activation = asyncio.run(
        bridge.activate(
            update,
            orchestrator=orchestrator,  # type: ignore[arg-type]
            prompt="继续处理",
            agent_session=AgentSession(session_id="maf-1"),
            activation_id="activation-2",
            at=at(11),
        )
    )

    assert activation == "activation-result"
    assert orchestrator.prompt is not None
    assert "delegation-a" in orchestrator.prompt
    assert '"source_event_payload":{"sequence":1}' in orchestrator.prompt
