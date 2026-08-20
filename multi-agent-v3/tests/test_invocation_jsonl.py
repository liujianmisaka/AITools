from __future__ import annotations

from pathlib import Path

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_contracts import (
    ArtifactRef,
    CompletionBoundary,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
)
from misaka_invocation_jsonl import JsonlInvocationStore
from misaka_invocation_runtime import IdempotencyConflict, InvocationError, InvocationRuntime
from misaka_persistence_contracts import DurableCorruption
from misaka_persistence_jsonl import JsonlEventLog


def _request(
    invocation_id: str = "inv-1",
    key: str = "key-1",
    *,
    prompt: str = "hello",
) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": prompt},
        idempotency_key=key,
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        model="pixel/gpt-5.6-luna",
        effort="high",
        output_schema={
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        },
    )


def _log(path: Path) -> JsonlEventLog:
    return JsonlEventLog(path)


@pytest.mark.asyncio
async def test_jsonl_invocation_store_replays_terminal_execution(tmp_path: Path) -> None:
    path = tmp_path / "invocations.jsonl"
    store = JsonlInvocationStore(_log(path))
    request = _request()
    request = InvocationRequest(
        invocation_id=request.invocation_id,
        capability_id=request.capability_id,
        operation=request.operation,
        input=request.input,
        idempotency_key=request.idempotency_key,
        completion_boundary=request.completion_boundary,
        model=request.model,
        effort=request.effort,
        output_schema=request.output_schema,
        owner_id="controller-1",
        scope_id="scope-1",
        lease_owner="execution-1",
        lease_epoch=4,
        resource_refs=("workspace:repo",),
    )

    created, is_created = await store.create(request)
    assert is_created
    assert created.status is InvocationStatus.REGISTERED

    await store.append_event(
        request.invocation_id,
        InvocationStatus.PREFLIGHTING,
        {"provider_id": "codex", "provider_epoch": 3},
    )
    await store.append_event(
        request.invocation_id,
        InvocationStatus.RESOURCE_ACQUIRING,
        {"provider_id": "codex", "provider_epoch": 3},
    )
    await store.append_event(
        request.invocation_id,
        InvocationStatus.PREPARED,
        {
            "provider_id": "codex",
            "provider_epoch": 3,
            "provider_session_id": "thread-1",
        },
    )
    await store.append_event(
        request.invocation_id,
        InvocationStatus.STARTING,
        {
            "provider_id": "codex",
            "provider_epoch": 3,
            "provider_session_id": "thread-1",
            "external_start_attempted": True,
        },
    )
    await store.append_event(
        request.invocation_id,
        InvocationStatus.RUNNING,
        {
            "provider_id": "codex",
            "provider_epoch": 3,
            "provider_session_id": "thread-1",
            "provider_operation_id": "turn-1",
            "external_start_attempted": True,
        },
    )
    await store.append_event(request.invocation_id, InvocationStatus.FINALIZING, {})
    result = InvocationResult(
        invocation_id=request.invocation_id,
        status=InvocationStatus.SUCCEEDED,
        output={"answer": "ok"},
        artifacts=(
            ArtifactRef(
                artifact_id="artifact-1",
                media_type="text/plain",
                size_bytes=2,
                sha256="a" * 64,
                location="artifacts/artifact-1",
            ),
        ),
    )
    await store.finalize(result)

    reopened = JsonlInvocationStore(_log(path))
    snapshot = await reopened.snapshot(request.invocation_id)

    assert snapshot.request == request
    assert snapshot.ownership == request.ownership
    assert snapshot.status is InvocationStatus.SUCCEEDED
    assert snapshot.result == result
    assert snapshot.activation_id == "inv-1:activation:1"
    assert snapshot.provider_execution is not None
    assert snapshot.provider_execution.provider_session_id == "thread-1"
    assert snapshot.provider_execution.provider_operation_id == "turn-1"
    assert snapshot.provider_execution.external_start_attempted
    assert [event.sequence for event in snapshot.events] == list(range(1, 9))


@pytest.mark.asyncio
async def test_jsonl_invocation_store_preserves_inflight_state_and_idempotency(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invocations.jsonl"
    store = JsonlInvocationStore(_log(path))
    request = _request()
    await store.create(request)
    await store.append_event(
        request.invocation_id,
        InvocationStatus.PREFLIGHTING,
        {"provider_id": "fake", "provider_epoch": 1},
    )

    duplicate, is_created = await store.create(request)
    assert not is_created
    assert duplicate.status is InvocationStatus.PREFLIGHTING

    with pytest.raises(IdempotencyConflict):
        await store.create(_request("other", key="key-1", prompt="different"))

    reopened = JsonlInvocationStore(_log(path))
    snapshots = await reopened.list()
    assert len(snapshots) == 1
    assert snapshots[0].status is InvocationStatus.PREFLIGHTING
    assert snapshots[0].provider_execution is not None
    assert snapshots[0].provider_execution.provider_id == "fake"


@pytest.mark.asyncio
async def test_jsonl_invocation_store_events_support_cursor_replay(tmp_path: Path) -> None:
    store = JsonlInvocationStore(_log(tmp_path / "invocations.jsonl"))
    request = _request()
    await store.create(request)
    await store.append_event(request.invocation_id, InvocationStatus.PREFLIGHTING, {})
    await store.append_event(request.invocation_id, InvocationStatus.STARTING, {})
    await store.finalize(
        InvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.CANCELLED)
    )

    events = [event async for event in store.events(request.invocation_id, start_sequence=3)]
    assert [event.sequence for event in events] == [3, 4]
    assert events[-1].status is InvocationStatus.CANCELLED


@pytest.mark.asyncio
async def test_jsonl_invocation_store_rejects_corrupt_stream(tmp_path: Path) -> None:
    path = tmp_path / "invocations.jsonl"
    log = _log(path)
    await log.append(
        "invocation:broken",
        "created:broken",
        "invocation.created",
        {"unexpected": True},
    )

    with pytest.raises(DurableCorruption) as raised:
        await JsonlInvocationStore(_log(path)).open()
    assert raised.value.code == "invocation.request_invalid"


@pytest.mark.asyncio
async def test_jsonl_invocation_store_rejects_events_after_terminal(tmp_path: Path) -> None:
    store = JsonlInvocationStore(_log(tmp_path / "invocations.jsonl"))
    request = _request()
    await store.create(request)
    await store.finalize(
        InvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED)
    )

    with pytest.raises(InvocationError) as raised:
        await store.append_event(request.invocation_id, InvocationStatus.RUNNING, {})
    assert raised.value.code == "invocation.already_terminal"


@pytest.mark.asyncio
async def test_invocation_runtime_can_use_jsonl_store_and_reopen_terminal_fact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.jsonl"
    request = InvocationRequest(
        invocation_id="runtime-invocation",
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "return the deterministic answer"},
        idempotency_key="runtime-key",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        output_schema={
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    store = JsonlInvocationStore(_log(path))
    runtime = InvocationRuntime(store=store)
    await runtime.register_provider(
        "fake",
        FakeAgentProvider(FakeAgentScenario(output={"answer": "ok"})),
    )

    result = await (await runtime.submit(request, provider_id="fake")).wait()
    await runtime.stop()

    assert result.status is InvocationStatus.SUCCEEDED
    reopened = JsonlInvocationStore(_log(path))
    snapshot = await reopened.snapshot(request.invocation_id)
    assert snapshot.result == result
    assert snapshot.status is InvocationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_jsonl_store_supports_safe_recovery_after_runtime_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.jsonl"
    request = InvocationRequest(
        invocation_id="restart-invocation",
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "return the deterministic answer"},
        idempotency_key="restart-key",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        output_schema={
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    first_store = JsonlInvocationStore(_log(path))
    await first_store.create(request)

    restarted_store = JsonlInvocationStore(_log(path))
    runtime = InvocationRuntime(store=restarted_store)
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "recovered"}))
    await runtime.register_provider("fake", provider)

    handles = await runtime.recover()
    result = await handles[0].wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "recovered"}
    assert provider.starts == 1
    await runtime.stop()
