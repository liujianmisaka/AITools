from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from misaka_approval_capability import (
    DecisionConflict,
    DecisionDenied,
    DecisionGate,
    DecisionRequired,
    MemoryDecisionStore,
)
from misaka_approval_jsonl import JsonlDecisionStore
from misaka_interaction_contracts import (
    DecisionProposal,
    DecisionRef,
    DecisionStatus,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_persistence_jsonl import JsonlEventLog


def _principal(principal_id: str, kind: PrincipalKind) -> PrincipalRef:
    return PrincipalRef(principal_id, kind)


def _proposal(
    *,
    revision: int = 1,
    plan_hash: str = "a" * 64,
    effects: tuple[str, ...] = ("workspace:write",),
) -> DecisionProposal:
    return DecisionProposal(
        ref=DecisionRef("proposal-1", revision),
        plan_hash=plan_hash,
        requested_effects=effects,
        scope=ScopeRef("scope-1"),
        created_by=_principal("caller", PrincipalKind.APPLICATION),
        payload={"instance_id": "instance-1"},
        policy_snapshot={"network": "deny"},
    )


@pytest.mark.asyncio
async def test_memory_decision_store_is_idempotent_and_single_decision() -> None:
    store = MemoryDecisionStore()
    proposal = _proposal()
    pending = await store.ensure(proposal)
    assert pending.status is DecisionStatus.PENDING
    assert await store.ensure(proposal) == pending

    decided = await store.decide(
        proposal.ref,
        status=DecisionStatus.APPROVED,
        decided_by=_principal("reviewer", PrincipalKind.HUMAN),
        reason="reviewed",
    )
    assert decided.status is DecisionStatus.APPROVED
    assert decided.fact is not None
    assert decided.fact.matches(proposal)
    assert (
        await store.decide(
            proposal.ref,
            status=DecisionStatus.APPROVED,
            decided_by=_principal("reviewer", PrincipalKind.HUMAN),
            reason="reviewed",
        )
        == decided
    )
    with pytest.raises(DecisionConflict, match="already terminal"):
        await store.decide(
            proposal.ref,
            status=DecisionStatus.REJECTED,
            decided_by=_principal("reviewer", PrincipalKind.HUMAN),
            reason="changed",
        )


@pytest.mark.asyncio
async def test_jsonl_decision_store_reopens_bound_durable_fact(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    log = JsonlEventLog(path)
    store = JsonlDecisionStore(log)
    proposal = _proposal()
    await store.ensure(proposal)
    await store.decide(
        proposal.ref,
        status=DecisionStatus.REJECTED,
        decided_by=_principal("reviewer", PrincipalKind.HUMAN),
        reason="unsafe",
    )
    await log.close()

    reopened = JsonlDecisionStore(JsonlEventLog(path))
    restored = await reopened.get(proposal.ref)
    assert restored.proposal == proposal
    assert restored.fact is not None
    assert restored.fact.status is DecisionStatus.REJECTED
    assert restored.fact.reason == "unsafe"
    assert restored.fact.decided_by.principal_id == "reviewer"


@pytest.mark.asyncio
async def test_same_revision_cannot_be_reused_for_changed_plan_or_effect_scope() -> None:
    store = MemoryDecisionStore()
    await store.ensure(_proposal())
    with pytest.raises(DecisionConflict, match="different plan or effect scope"):
        await store.ensure(_proposal(plan_hash="b" * 64))
    with pytest.raises(DecisionConflict, match="different plan or effect scope"):
        await store.ensure(_proposal(effects=("process:start",)))


@pytest.mark.asyncio
async def test_old_approval_cannot_authorize_a_new_revision() -> None:
    store = MemoryDecisionStore()
    gate = DecisionGate(store)
    first = _proposal(revision=1)
    second = _proposal(revision=2)
    await store.ensure(first)
    await store.decide(
        first.ref,
        status=DecisionStatus.APPROVED,
        decided_by=_principal("reviewer", PrincipalKind.HUMAN),
    )

    assert (await gate.authorize(first)).status is DecisionStatus.APPROVED
    with pytest.raises(DecisionRequired):
        await gate.authorize(second)

    await store.decide(
        second.ref,
        status=DecisionStatus.REJECTED,
        decided_by=_principal("reviewer", PrincipalKind.HUMAN),
        reason="new plan is unsafe",
    )
    with pytest.raises(DecisionDenied, match="unsafe"):
        await gate.authorize(second)


@pytest.mark.asyncio
async def test_jsonl_decision_store_serializes_conflicting_decisions(tmp_path: Path) -> None:
    store = JsonlDecisionStore(JsonlEventLog(tmp_path / "decisions.jsonl"))
    proposal = _proposal()
    await store.ensure(proposal)

    results = await asyncio.gather(
        store.decide(
            proposal.ref,
            status=DecisionStatus.APPROVED,
            decided_by=_principal("reviewer-a", PrincipalKind.HUMAN),
        ),
        store.decide(
            proposal.ref,
            status=DecisionStatus.REJECTED,
            decided_by=_principal("reviewer-b", PrincipalKind.HUMAN),
            reason="rejected",
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, DecisionConflict) for item in results) == 1
    assert sum(not isinstance(item, BaseException) for item in results) == 1
