from __future__ import annotations

import asyncio
from datetime import datetime

from misaka_approval_capability import (
    ApprovalConflict,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStatus,
)
from misaka_persistence_jsonl import JsonlEventLog


class JsonlApprovalStore:
    _STREAM = "capability.approvals"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._approvals: dict[str, ApprovalRecord] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        async with self._lock:
            await self._open_unlocked()

    async def _open_unlocked(self) -> None:
        if self._loaded:
            return
        for event in await self._log.read(self._STREAM):
            payload = event.payload
            approval_id = str(payload["approval_id"])
            if event.event_type == "approval.created":
                record = ApprovalRecord(
                    ApprovalRequest(
                        approval_id=approval_id,
                        instance_id=str(payload["instance_id"]),
                        created_at=datetime.fromisoformat(str(payload["created_at"])),
                    ),
                    ApprovalStatus.PENDING,
                )
            elif event.event_type == "approval.decided":
                current = self._approvals.get(approval_id)
                if current is None:
                    raise ApprovalConflict(
                        "approval.history_invalid",
                        f"approval {approval_id} decision has no request",
                    )
                decision = ApprovalDecision(
                    ApprovalDecisionValue(str(payload["decision"])),
                    reason=str(payload.get("reason", "")),
                    decided_at=datetime.fromisoformat(str(payload["decided_at"])),
                )
                record = ApprovalRecord(current.request, ApprovalStatus.DECIDED, decision)
            else:
                raise ApprovalConflict(
                    "approval.event_unknown", f"unknown approval event {event.event_type}"
                )
            self._approvals[approval_id] = record
        self._loaded = True

    async def ensure(self, request: ApprovalRequest) -> ApprovalRecord:
        async with self._lock:
            await self._open_unlocked()
            existing = self._approvals.get(request.approval_id)
            if existing is not None:
                if existing.request.instance_id != request.instance_id:
                    raise ApprovalConflict(
                        "approval.instance_conflict",
                        f"approval {request.approval_id} belongs to another instance",
                    )
                return existing
            await self._log.append(
                self._STREAM,
                f"approval-created:{request.approval_id}",
                "approval.created",
                {
                    "approval_id": request.approval_id,
                    "instance_id": request.instance_id,
                    "created_at": request.created_at.isoformat(),
                },
            )
            record = ApprovalRecord(request, ApprovalStatus.PENDING)
            self._approvals[request.approval_id] = record
            return record

    async def get(self, approval_id: str) -> ApprovalRecord:
        async with self._lock:
            await self._open_unlocked()
            try:
                return self._approvals[approval_id]
            except KeyError as exc:
                raise ApprovalNotFound(
                    "approval.not_found", f"approval {approval_id} was not found"
                ) from exc

    async def list(self) -> tuple[ApprovalRecord, ...]:
        async with self._lock:
            await self._open_unlocked()
            return tuple(
                sorted(self._approvals.values(), key=lambda item: item.request.approval_id)
            )

    async def decide(self, approval_id: str, decision: ApprovalDecision) -> ApprovalRecord:
        async with self._lock:
            await self._open_unlocked()
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
                    "approval.already_decided",
                    f"approval {approval_id} is already decided",
                )
            await self._log.append(
                self._STREAM,
                f"approval-decided:{approval_id}",
                "approval.decided",
                {
                    "approval_id": approval_id,
                    "decision": decision.value.value,
                    "reason": decision.reason,
                    "decided_at": decision.decided_at.isoformat(),
                },
            )
            updated = ApprovalRecord(current.request, ApprovalStatus.DECIDED, decision)
            self._approvals[approval_id] = updated
            return updated
