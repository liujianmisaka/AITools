from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationMode,
    DelegationRef,
    DelegationReport,
    DelegationRequest,
    DelegationStatus,
)
from misaka_delegation_jsonl import JsonlDelegationStore
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableCorruption
from misaka_persistence_jsonl import JsonlEventLog


def _principal(principal_id: str = "parent") -> PrincipalRef:
    return PrincipalRef(principal_id, PrincipalKind.APPLICATION)


def _request(delegation_id: str = "delegation-1") -> DelegationRequest:
    return DelegationRequest(
        delegation_id=delegation_id,
        idempotency_key=f"idem-{delegation_id}",
        initiator=_principal(),
        controller=_principal(),
        scope=ScopeRef("scope-1"),
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "inspect"},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        output_schema={"type": "object"},
        mode=DelegationMode.CONTINUABLE,
    )


@pytest.mark.asyncio
async def test_jsonl_delegation_store_rebuilds_activation_and_report_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delegations.jsonl"
    request = _request()
    ref = DelegationRef(
        request.delegation_id,
        session_id="session-1",
        channel_id="channel-1",
    )
    store = JsonlDelegationStore(JsonlEventLog(path))
    created, was_created = await store.create(request, ref)
    assert was_created is True
    await store.activate(request.delegation_id, "delegation-1:activation:1")
    first = await store.finalize(
        request.delegation_id,
        DelegationReport(
            delegation_id=request.delegation_id,
            status=DelegationStatus.COMPLETED,
            output={"answer": "first"},
            source_activation_id="delegation-1:activation:1",
        ),
    )
    await store.activate(request.delegation_id, "delegation-1:activation:2")
    second = await store.finalize(
        request.delegation_id,
        DelegationReport(
            delegation_id=request.delegation_id,
            status=DelegationStatus.COMPLETED,
            output={"answer": "second"},
            source_activation_id="delegation-1:activation:2",
        ),
    )

    reopened = JsonlDelegationStore(JsonlEventLog(path))
    restored = await reopened.snapshot(request.delegation_id)

    assert created.ref == ref
    assert first.report is not None
    assert second.report is not None
    assert restored.report == second.report
    assert restored.report_history == (first.report, second.report)
    assert restored.activation_count == 2


@pytest.mark.asyncio
async def test_jsonl_delegation_store_replays_continuation_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "delegations.jsonl"
    request = _request("delegation-idem")
    ref = DelegationRef(request.delegation_id, session_id="session", channel_id="channel")
    store = JsonlDelegationStore(JsonlEventLog(path))
    await store.create(request, ref)
    continuation = ContinuationRequest(
        request_id="continuation-1",
        delegation_id=request.delegation_id,
        operation=ContinuationOperation.FOLLOW_UP,
        actor=_principal(),
        idempotency_key="continuation-key",
        session_id="session",
        message_id="message-1",
        input={"prompt": "continue"},
    )
    assert (
        await store.claim_continuation(
            request.delegation_id,
            continuation.idempotency_key,
            "fingerprint-1",
        )
        is True
    )
    assert (
        await store.claim_continuation(
            request.delegation_id,
            continuation.idempotency_key,
            "fingerprint-1",
        )
        is False
    )

    reopened = JsonlDelegationStore(JsonlEventLog(path))
    assert (
        await reopened.claim_continuation(
            request.delegation_id,
            continuation.idempotency_key,
            "fingerprint-1",
        )
        is False
    )


@pytest.mark.asyncio
async def test_jsonl_delegation_store_rejects_duplicate_creation_fact(tmp_path: Path) -> None:
    path = tmp_path / "delegations.jsonl"
    log = JsonlEventLog(path)
    request = _request("delegation-duplicate")
    ref = DelegationRef(request.delegation_id)
    payload = cast(
        JsonObject,
        {
            "request": {
                "delegation_id": request.delegation_id,
                "idempotency_key": request.idempotency_key,
                "initiator": {
                    "principal_id": "parent",
                    "kind": "application",
                    "display_name": "",
                },
                "controller": {
                    "principal_id": "parent",
                    "kind": "application",
                    "display_name": "",
                },
                "scope": {"scope_id": "scope-1", "parent_scope_id": None},
                "capability_id": "agent.invocation",
                "operation": "invoke",
                "input": {"prompt": "inspect"},
                "provider_id": "fake-agent",
                "model": "fake/model",
                "effort": "high",
                "output_schema": {"type": "object"},
                "mode": "continuable",
                "parent_delegation_id": None,
                "session_id": None,
                "channel_id": None,
                "decision_ref": None,
                "required_features": [],
                "constraints": {},
            },
            "ref": {
                "delegation_id": ref.delegation_id,
                "session_id": None,
                "channel_id": None,
                "parent_delegation_id": None,
            },
        },
    )
    await log.append("delegation:delegation-duplicate", "created-1", "delegation.created", payload)
    await log.append("delegation:delegation-duplicate", "created-2", "delegation.created", payload)

    with pytest.raises(DurableCorruption, match="duplicate creation"):
        await JsonlDelegationStore(JsonlEventLog(path)).open()
