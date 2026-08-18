from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from misaka_approval_capability import (
    ApprovalConflict,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    ApprovalStatus,
    MemoryApprovalStore,
)
from misaka_approval_jsonl import JsonlApprovalStore
from misaka_persistence_jsonl import JsonlEventLog


@pytest.mark.asyncio
async def test_memory_approval_store_is_idempotent_and_single_decision() -> None:
    store = MemoryApprovalStore()
    request = ApprovalRequest("approval-1", "instance-1")
    pending = await store.ensure(request)
    assert pending.status is ApprovalStatus.PENDING
    assert await store.ensure(request) == pending

    decision = ApprovalDecision(ApprovalDecisionValue.APPROVE, reason="reviewed")
    decided = await store.decide(request.approval_id, decision)
    assert decided.status is ApprovalStatus.DECIDED
    assert await store.decide(request.approval_id, decision) == decided
    with pytest.raises(ApprovalConflict, match="already decided"):
        await store.decide(
            request.approval_id,
            ApprovalDecision(ApprovalDecisionValue.REJECT, reason="changed"),
        )


@pytest.mark.asyncio
async def test_jsonl_approval_store_reopens_durable_decision(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    log = JsonlEventLog(path)
    store = JsonlApprovalStore(log)
    request = ApprovalRequest("approval-1", "instance-1")
    await store.ensure(request)
    decision = ApprovalDecision(ApprovalDecisionValue.REJECT, reason="unsafe")
    await store.decide(request.approval_id, decision)
    await log.close()

    reopened = JsonlApprovalStore(JsonlEventLog(path))
    restored = await reopened.get(request.approval_id)
    assert restored.request.instance_id == "instance-1"
    assert restored.decision is not None
    assert restored.decision.value is ApprovalDecisionValue.REJECT
    assert restored.decision.reason == "unsafe"


@pytest.mark.asyncio
async def test_approval_id_cannot_be_reused_for_another_instance() -> None:
    store = MemoryApprovalStore()
    await store.ensure(ApprovalRequest("approval-1", "instance-1"))
    with pytest.raises(ApprovalConflict, match="another instance"):
        await store.ensure(ApprovalRequest("approval-1", "instance-2"))


@pytest.mark.asyncio
async def test_jsonl_approval_store_serializes_conflicting_decisions(tmp_path: Path) -> None:
    store = JsonlApprovalStore(JsonlEventLog(tmp_path / "approval.jsonl"))
    request = ApprovalRequest("approval-1", "instance-1")
    await store.ensure(request)

    results = await asyncio.gather(
        store.decide(
            request.approval_id,
            ApprovalDecision(ApprovalDecisionValue.APPROVE, reason="approved"),
        ),
        store.decide(
            request.approval_id,
            ApprovalDecision(ApprovalDecisionValue.REJECT, reason="rejected"),
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, ApprovalConflict) for item in results) == 1
    assert sum(not isinstance(item, BaseException) for item in results) == 1
