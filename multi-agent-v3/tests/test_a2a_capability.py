from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from misaka_a2a_capability import (
    A2AAgentCard,
    A2ASkill,
    MemoryTaskStore,
    TaskIdempotencyConflict,
    TaskRequest,
    TaskResult,
    TaskStateError,
    TaskStatus,
)
from misaka_invocation_contracts import CapabilityFeature


def _request(
    task_id: str = "task-1",
    *,
    idempotency_key: str = "idem-1",
    value: str = "hello",
) -> TaskRequest:
    return TaskRequest(
        task_id=task_id,
        context_id="context-1",
        message_id="message-1",
        idempotency_key=idempotency_key,
        capability_id="agent.execute",
        operation="invoke",
        input={"prompt": value},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        required_features=frozenset({CapabilityFeature.STREAMING}),
    )


def test_agent_card_requires_unique_skills() -> None:
    skill = A2ASkill(
        skill_id="agent.invoke",
        name="Invoke agent",
        description="Run one explicit agent invocation",
        capability_id="agent.execute",
        operation="invoke",
    )
    with pytest.raises(ValueError, match="skill ids must be unique"):
        A2AAgentCard(
            agent_id="test-agent",
            name="Test agent",
            description="Test card",
            version="1.0.0",
            skills=(skill, skill),
        )


@pytest.mark.asyncio
async def test_memory_task_store_is_idempotent_across_client_task_ids() -> None:
    store = MemoryTaskStore()
    first, first_created = await store.create(_request())
    duplicate, duplicate_created = await store.create(_request("task-retry"))

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.request.task_id == first.request.task_id == "task-1"
    assert duplicate.events == first.events


@pytest.mark.asyncio
async def test_memory_task_store_rejects_idempotency_key_reuse() -> None:
    store = MemoryTaskStore()
    await store.create(_request())

    with pytest.raises(TaskIdempotencyConflict, match="different request"):
        await store.create(_request("task-2", value="different"))


@pytest.mark.asyncio
async def test_memory_task_store_stream_reconnects_from_sequence() -> None:
    store = MemoryTaskStore()
    await store.create(_request())
    await store.mark_working("task-1", "invocation-1")
    await store.append_event(
        "task-1",
        TaskStatus.WORKING,
        {"type": "agent.progress", "percent": 50},
    )
    await store.finalize(
        TaskResult(
            task_id="task-1",
            invocation_id="invocation-1",
            status=TaskStatus.COMPLETED,
            output={"answer": "ok"},
        )
    )

    events = [event async for event in store.events("task-1", start_sequence=3)]

    assert [event.sequence for event in events] == [3, 4]
    assert events[-1].status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_memory_task_store_waits_for_terminal_result() -> None:
    store = MemoryTaskStore()
    await store.create(_request())
    waiter = asyncio.create_task(store.wait_terminal("task-1"))
    await asyncio.sleep(0)
    await store.finalize(
        TaskResult(
            task_id="task-1",
            invocation_id=None,
            status=TaskStatus.REJECTED,
            error_code="a2a.rejected",
            error_message="request rejected before activation",
        )
    )

    assert (await waiter).status is TaskStatus.REJECTED


@pytest.mark.asyncio
async def test_memory_task_store_rejects_conflicting_invocation_and_terminal_rewrite() -> None:
    store = MemoryTaskStore()
    await store.create(_request())
    await store.mark_working("task-1", "invocation-1")

    with pytest.raises(TaskStateError, match="another invocation"):
        await store.mark_working("task-1", "invocation-2")

    result = TaskResult(
        task_id="task-1",
        invocation_id="invocation-1",
        status=TaskStatus.COMPLETED,
        output={"answer": "ok"},
    )
    await store.finalize(result)
    assert (await store.finalize(result)).result == result

    with pytest.raises(TaskStateError, match="different terminal result"):
        await store.finalize(replace(result, output={"answer": "changed"}))
