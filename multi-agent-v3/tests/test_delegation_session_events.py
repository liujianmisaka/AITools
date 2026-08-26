from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationMode,
    DelegationRequest,
)
from misaka_delegation_runtime import (
    DelegationRuntime,
    DelegationSessionEvent,
    DelegationSessionEventKind,
    DelegationSessionEventStore,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_interaction_contracts import MessageType, PrincipalKind, PrincipalRef, ScopeRef
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_invocation_runtime import InvocationRuntime
from misaka_persistence_jsonl import JsonlEventLog


def _request(delegation_id: str) -> DelegationRequest:
    principal = PrincipalRef("controller", PrincipalKind.APPLICATION)
    return DelegationRequest(
        delegation_id=delegation_id,
        idempotency_key=f"idempotency:{delegation_id}",
        initiator=principal,
        controller=principal,
        scope=ScopeRef("scope"),
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "say hello", "cwd": str(Path.cwd())},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
    )


async def _next_event(
    stream: AsyncIterator[DelegationSessionEvent],
) -> DelegationSessionEvent:
    return await anext(stream)


@pytest.mark.asyncio
async def test_session_event_store_waits_orders_and_replays(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = DelegationSessionEventStore(JsonlEventLog(path))
    pending = store.events("delegation-1")
    first = asyncio.create_task(_next_event(pending))
    await asyncio.sleep(0)

    await store.publish(
        delegation_id="delegation-1",
        event_id="created",
        kind=DelegationSessionEventKind.LIFECYCLE,
        status="proposed",
        payload={"stage": "created"},
    )
    assert (await first).sequence == 1

    second = asyncio.create_task(_next_event(pending))
    await asyncio.sleep(0)
    await store.publish(
        delegation_id="delegation-1",
        event_id="delta",
        kind=DelegationSessionEventKind.OUTPUT_DELTA,
        invocation_id="invocation-1",
        activation_id="activation-1",
        activation_number=1,
        status="running",
        payload={"text": "hello"},
    )
    assert (await second).payload == {"text": "hello"}

    await store.close_session("delegation-1", event_id="closed", status="completed")
    inspection = await store.inspect("delegation-1")
    assert inspection.last_sequence == 3
    assert inspection.closed is True
    assert (await anext(pending)).kind is DelegationSessionEventKind.SESSION_CLOSED
    with pytest.raises(StopAsyncIteration):
        await anext(pending)

    replayed = DelegationSessionEventStore(JsonlEventLog(path))
    events = await replayed.read("delegation-1")
    assert [event.kind for event in events] == [
        DelegationSessionEventKind.LIFECYCLE,
        DelegationSessionEventKind.OUTPUT_DELTA,
        DelegationSessionEventKind.SESSION_CLOSED,
    ]


@pytest.mark.asyncio
async def test_session_event_store_rejects_conflicting_event_id_reuse() -> None:
    store = DelegationSessionEventStore()
    first = await store.publish(
        delegation_id="delegation-idempotency",
        event_id="event-1",
        kind=DelegationSessionEventKind.LIFECYCLE,
        status="running",
        payload={"stage": "activation_started"},
    )

    assert (
        await store.publish(
            delegation_id="delegation-idempotency",
            event_id="event-1",
            kind=DelegationSessionEventKind.LIFECYCLE,
            status="running",
            payload={"stage": "activation_started"},
        )
    ) == first
    with pytest.raises(ValueError, match="conflicts with existing content"):
        await store.publish(
            delegation_id="delegation-idempotency",
            event_id="event-1",
            kind=DelegationSessionEventKind.LIFECYCLE,
            status="completed",
            payload={"stage": "terminal"},
        )


@pytest.mark.asyncio
async def test_runtime_projects_public_agent_output_without_raw_provider_payload() -> None:
    provider = FakeAgentProvider(
        FakeAgentScenario(
            output={"answer": "hello"},
            events=(
                {
                    "type": "agent.message.delta",
                    "text": "hello",
                    "reasoning": "must not be projected",
                },
                {
                    "type": "agent.message.completed",
                    "text": "hello",
                    "phase": "final_answer",
                },
                {
                    "type": "agent.tool.started",
                    "item_id": "tool-1",
                    "tool_name": "Read",
                    "tool_use_id": "tool-1",
                    "input": {"secret": "must not be projected"},
                },
                {
                    "type": "agent.command.output.delta",
                    "item_id": "command-1",
                    "stream": "stdout",
                    "text": "1 passed",
                },
                {
                    "type": "agent.plan.completed",
                    "item_id": "plan-1",
                    "plan": [{"step": "Run tests", "status": "completed", "raw": "drop"}],
                },
                {
                    "type": "agent.question",
                    "question_id": "question-1",
                    "text": "Proceed with the migration?",
                    "options": ["yes", "no"],
                },
            ),
        )
    )
    invocation_runtime = InvocationRuntime()
    await invocation_runtime.register_provider("fake-agent", provider)
    session_events = DelegationSessionEventStore()
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(
        invocation_runtime,
        channels,
        session_events=session_events,
    )

    handle = await runtime.submit(_request("delegation-public-events"))
    report = await handle.wait()
    assert report.status.value == "completed"

    events = await session_events.read("delegation-public-events")
    kinds = [event.kind for event in events]
    assert kinds.count(DelegationSessionEventKind.OUTPUT_DELTA) == 1
    assert kinds.count(DelegationSessionEventKind.OUTPUT_COMPLETED) == 1
    assert kinds.count(DelegationSessionEventKind.TOOL_STARTED) == 1
    assert kinds.count(DelegationSessionEventKind.COMMAND_OUTPUT_DELTA) == 1
    assert kinds.count(DelegationSessionEventKind.PLAN_COMPLETED) == 1
    assert kinds.count(DelegationSessionEventKind.AGENT_QUESTION) == 1
    assert kinds[-2:] == [
        DelegationSessionEventKind.TERMINAL,
        DelegationSessionEventKind.SESSION_CLOSED,
    ]
    delta = next(event for event in events if event.kind is DelegationSessionEventKind.OUTPUT_DELTA)
    assert delta.payload == {
        "provider_event_type": "agent.message.delta",
        "text": "hello",
    }
    assert "reasoning" not in delta.payload
    tool = next(event for event in events if event.kind is DelegationSessionEventKind.TOOL_STARTED)
    assert tool.payload == {
        "provider_event_type": "agent.tool.started",
        "item_id": "tool-1",
        "tool_name": "Read",
        "tool_use_id": "tool-1",
    }
    command = next(
        event for event in events if event.kind is DelegationSessionEventKind.COMMAND_OUTPUT_DELTA
    )
    assert command.payload["text"] == "1 passed"
    plan = next(
        event for event in events if event.kind is DelegationSessionEventKind.PLAN_COMPLETED
    )
    assert plan.payload["plan"] == [{"step": "Run tests", "status": "completed"}]
    assert events[-2].payload["output"] == {"answer": "hello"}

    await runtime.stop()
    await invocation_runtime.stop()


@pytest.mark.asyncio
async def test_continuable_session_events_keep_one_cursor_across_activations() -> None:
    provider = FakeAgentProvider(
        FakeAgentScenario(
            output={"answer": "hello"},
            events=({"type": "agent.message.delta", "text": "hello"},),
        )
    )
    invocation_runtime = InvocationRuntime()
    await invocation_runtime.register_provider("fake-agent", provider)
    session_events = DelegationSessionEventStore()
    runtime = DelegationRuntime(
        invocation_runtime,
        MemoryInteractionChannelStore(),
        session_events=session_events,
    )
    request = replace(_request("delegation-multi-activation"), mode=DelegationMode.CONTINUABLE)
    try:
        handle = await runtime.submit(request)
        first_report = await handle.wait()
        first_snapshot = await handle.snapshot()
        assert first_report.source_activation_id is not None
        second = await handle.continue_request(
            ContinuationRequest(
                request_id="multi-activation-follow-up",
                delegation_id=request.delegation_id,
                operation=ContinuationOperation.FOLLOW_UP,
                actor=request.controller,
                idempotency_key="multi-activation-follow-up-key",
                session_id=first_snapshot.ref.session_id,
                message_id="multi-activation-follow-up-message",
                expected_activation_id=first_report.source_activation_id,
                input={"prompt": "continue"},
            )
        )
        await second.wait()
        events = await session_events.read(request.delegation_id)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert {event.activation_number for event in events if event.activation_number} == {1, 2}
        assert not any(event.kind is DelegationSessionEventKind.SESSION_CLOSED for event in events)
        inputs = [
            event for event in events if event.kind is DelegationSessionEventKind.INPUT_MESSAGE
        ]
        assert [event.payload["text"] for event in inputs] == ["say hello", "continue"]
    finally:
        await runtime.stop()
        await invocation_runtime.stop()


@pytest.mark.asyncio
async def test_continuable_agent_question_becomes_a_replyable_interaction_message() -> None:
    provider = FakeAgentProvider(
        FakeAgentScenario(
            output={"answer": "waiting"},
            events=(
                {
                    "type": "agent.question",
                    "question_id": "question-1",
                    "text": "Proceed with the migration?",
                    "options": ["yes", "no"],
                },
            ),
        )
    )
    invocation_runtime = InvocationRuntime()
    await invocation_runtime.register_provider("fake-agent", provider)
    session_events = DelegationSessionEventStore()
    runtime = DelegationRuntime(
        invocation_runtime,
        MemoryInteractionChannelStore(),
        session_events=session_events,
    )
    request = replace(_request("delegation-agent-question"), mode=DelegationMode.CONTINUABLE)
    try:
        handle = await runtime.submit(request)
        await handle.wait()

        questions = [
            message
            for message in await runtime.read_messages(request.delegation_id)
            if message.message_type is MessageType.QUESTION
        ]
        assert len(questions) == 1
        assert questions[0].correlation_id == "question-1"
        assert questions[0].payload["text"] == "Proceed with the migration?"
        assert questions[0].payload["options"] == ["yes", "no"]
        question_event = next(
            event
            for event in await session_events.read(request.delegation_id)
            if event.kind is DelegationSessionEventKind.AGENT_QUESTION
        )
        assert question_event.payload["message_id"] == questions[0].message_id
        assert question_event.payload["correlation_id"] == "question-1"
    finally:
        await runtime.stop()
        await invocation_runtime.stop()
