from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_delegation_contracts import DelegationRequest
from misaka_delegation_runtime import (
    DelegationRuntime,
    DelegationSessionEvent,
    DelegationSessionEventKind,
    DelegationSessionEventStore,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
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

    handle = await runtime.submit(_request("delegation-public-events"))
    report = await handle.wait()
    assert report.status.value == "completed"

    events = await session_events.read("delegation-public-events")
    kinds = [event.kind for event in events]
    assert kinds.count(DelegationSessionEventKind.OUTPUT_DELTA) == 1
    assert kinds.count(DelegationSessionEventKind.OUTPUT_COMPLETED) == 1
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
    assert events[-2].payload["output"] == {"answer": "hello"}

    await runtime.stop()
    await invocation_runtime.stop()
