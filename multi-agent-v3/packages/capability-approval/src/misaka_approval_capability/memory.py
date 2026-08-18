from __future__ import annotations

import asyncio

from misaka_approval_capability.contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStatus,
)
from misaka_approval_capability.errors import ApprovalConflict, ApprovalNotFound


class MemoryApprovalStore:
    def __init__(self) -> None:
        self._approvals: dict[str, ApprovalRecord] = {}
        self._lock = asyncio.Lock()

    async def ensure(self, request: ApprovalRequest) -> ApprovalRecord:
        async with self._lock:
            existing = self._approvals.get(request.approval_id)
            if existing is not None:
                if existing.request.instance_id != request.instance_id:
                    raise ApprovalConflict(
                        "approval.instance_conflict",
                        f"approval {request.approval_id} belongs to another instance",
                    )
                return existing
            record = ApprovalRecord(request, ApprovalStatus.PENDING)
            self._approvals[request.approval_id] = record
            return record

    async def get(self, approval_id: str) -> ApprovalRecord:
        async with self._lock:
            try:
                return self._approvals[approval_id]
            except KeyError as exc:
                raise ApprovalNotFound(
                    "approval.not_found", f"approval {approval_id} was not found"
                ) from exc

    async def list(self) -> tuple[ApprovalRecord, ...]:
        async with self._lock:
            return tuple(
                sorted(self._approvals.values(), key=lambda item: item.request.approval_id)
            )

    async def decide(self, approval_id: str, decision: ApprovalDecision) -> ApprovalRecord:
        async with self._lock:
            current = self._approvals.get(approval_id)
            if current is None:
                raise ApprovalNotFound(
                    "approval.not_found", f"approval {approval_id} was not found"
                )
            if current.decision is not None:
                if (
                    current.decision.value is decision.value
                    and current.decision.reason == decision.reason
                ):
                    return current
                raise ApprovalConflict(
                    "approval.already_decided", f"approval {approval_id} is already decided"
                )
            updated = ApprovalRecord(current.request, ApprovalStatus.DECIDED, decision)
            self._approvals[approval_id] = updated
            return updated
