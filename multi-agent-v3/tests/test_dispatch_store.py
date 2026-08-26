from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from misaka_delegation_capability import (
    AllowAllDelegationGate,
    DispatchConflict,
    DispatchNotFound,
    validate_dispatch_transition,
)
from misaka_delegation_contracts import (
    DelegationMode,
    DelegationRef,
    DelegationRequest,
    MessageDispatchMode,
    MessageDispatchRequest,
    MessageDispatchStatus,
    MessageDispatchStrategy,
    MessageDispatchTransition,
)
from misaka_delegation_jsonl import JsonlDelegationStore
from misaka_delegation_runtime.store import MemoryDelegationStore
from misaka_interaction_contracts import MessageType, PrincipalKind, PrincipalRef, ScopeRef
from misaka_persistence_jsonl import JsonlEventLog


def _principal(value: str = "caller") -> PrincipalRef:
    return PrincipalRef(value, PrincipalKind.APPLICATION)


def _request() -> DelegationRequest:
    return DelegationRequest(
        delegation_id="delegation-1",
        idempotency_key="delegation-key",
        initiator=_principal(),
        controller=_principal(),
        scope=ScopeRef("scope-1"),
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "inspect"},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        mode=DelegationMode.CONTINUABLE,
    )


def _dispatch() -> MessageDispatchRequest:
    return MessageDispatchRequest(
        dispatch_id="dispatch-1",
        delegation_id="delegation-1",
        idempotency_key="dispatch-key",
        message_id="message-1",
        actor=_principal(),
        session_id="session-1",
        expected_activation_id="activation-1",
        delivery=MessageDispatchMode.INTERRUPT_CONTINUE,
        message_type=MessageType.INSTRUCTION,
        payload={"text": "change direction"},
    )


async def _admit(store: MemoryDelegationStore | JsonlDelegationStore) -> None:
    request = _request()
    await store.create(request, DelegationRef(request.delegation_id, session_id="session-1"))
    admission = await AllowAllDelegationGate().evaluate(request, None)
    await store.record_admission(request.delegation_id, admission)


@pytest.mark.asyncio
async def test_memory_dispatch_is_idempotent_and_fenced() -> None:
    store = MemoryDelegationStore()
    await _admit(store)
    request = _dispatch()

    created, was_created = await store.create_dispatch(request)
    duplicate, was_duplicate = await store.create_dispatch(request)

    assert was_created is True
    assert was_duplicate is False
    assert duplicate == created

    with pytest.raises(DispatchConflict, match="different request"):
        await store.create_dispatch(replace(request, dispatch_id="dispatch-2"))

    queued = await store.transition_dispatch(
        request.delegation_id,
        request.dispatch_id,
        MessageDispatchTransition(
            status=MessageDispatchStatus.QUEUED,
            expected_status=MessageDispatchStatus.ACCEPTED,
            applied_strategy=MessageDispatchStrategy.QUEUED_FOR_NEXT_ACTIVATION,
        ),
    )
    assert queued.status is MessageDispatchStatus.QUEUED

    completed = await store.transition_dispatch(
        request.delegation_id,
        request.dispatch_id,
        MessageDispatchTransition(
            status=MessageDispatchStatus.DISPATCHING,
            expected_status=MessageDispatchStatus.QUEUED,
        ),
    )
    assert completed.status is MessageDispatchStatus.DISPATCHING

    with pytest.raises(DispatchConflict) as raised:
        validate_dispatch_transition(
            completed.status,
            MessageDispatchTransition(
                status=MessageDispatchStatus.COMPLETED,
                expected_status=MessageDispatchStatus.ACCEPTED,
                applied_strategy=MessageDispatchStrategy.STARTED_NEW_ACTIVATION,
            ),
        )
    assert raised.value.code == "message_dispatch.expected_status_mismatch"


@pytest.mark.asyncio
async def test_jsonl_dispatch_rebuilds_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "delegations.jsonl"
    first = JsonlDelegationStore(JsonlEventLog(path))
    await _admit(first)
    dispatch = _dispatch()
    await first.create_dispatch(dispatch)
    await first.transition_dispatch(
        dispatch.delegation_id,
        dispatch.dispatch_id,
        MessageDispatchTransition(
            status=MessageDispatchStatus.DISPATCHING,
            expected_status=MessageDispatchStatus.ACCEPTED,
        ),
    )

    reopened = JsonlDelegationStore(JsonlEventLog(path))
    restored = await reopened.dispatch(dispatch.delegation_id, dispatch.dispatch_id)

    assert restored.status is MessageDispatchStatus.DISPATCHING
    assert (await reopened.list_dispatches(dispatch.delegation_id)) == (restored,)


@pytest.mark.asyncio
async def test_jsonl_dispatch_does_not_publish_memory_state_before_durable_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "delegations-failing.jsonl"
    log = JsonlEventLog(path)
    store = JsonlDelegationStore(log)
    await _admit(store)

    def fail_append(_line: str) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(log, "_append_line", fail_append)

    with pytest.raises(OSError, match="disk unavailable"):
        await store.create_dispatch(_dispatch())

    with pytest.raises(DispatchNotFound):
        await store.dispatch("delegation-1", "dispatch-1")


@pytest.mark.asyncio
async def test_jsonl_dispatch_rejects_session_mismatch_before_durable_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delegations-session-mismatch.jsonl"
    store = JsonlDelegationStore(JsonlEventLog(path))
    await _admit(store)

    with pytest.raises(DispatchConflict) as raised:
        await store.create_dispatch(replace(_dispatch(), session_id="session-2"))

    assert raised.value.code == "message_dispatch.session_conflict"
    assert "delegation.message_dispatch.accepted" not in path.read_text(encoding="utf-8")
