from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from misaka_persistence_contracts import DurableConflict
from misaka_persistence_jsonl import JsonlEventLog

from misaka_control_plane.models import ApprovalDecisionSubmission


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    instance_id: str
    status: str
    decision: str | None
    reason: str | None
    created_at: datetime
    decided_at: datetime | None = None


class JsonlApprovalRegistry:
    _STREAM = "control.approvals"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._approvals: dict[str, ApprovalRecord] = {}
        self._loaded = False

    async def open(self) -> None:
        if self._loaded:
            return
        for event in await self._log.read(self._STREAM):
            payload = event.payload
            if event.event_type == "approval.created":
                record = ApprovalRecord(
                    approval_id=str(payload["approval_id"]),
                    instance_id=str(payload["instance_id"]),
                    status="pending",
                    decision=None,
                    reason=None,
                    created_at=datetime.fromisoformat(str(payload["created_at"])),
                )
            elif event.event_type == "approval.decided":
                current = self._approvals.get(str(payload["approval_id"]))
                if current is None:
                    raise DurableConflict("control.approval_missing", str(payload["approval_id"]))
                record = ApprovalRecord(
                    approval_id=current.approval_id,
                    instance_id=current.instance_id,
                    status="decided",
                    decision=str(payload["decision"]),
                    reason=str(payload.get("reason", "")),
                    created_at=current.created_at,
                    decided_at=datetime.fromisoformat(str(payload["decided_at"])),
                )
            else:
                raise DurableConflict("control.unknown_approval_event", event.event_type)
            self._approvals[record.approval_id] = record
        self._loaded = True

    async def ensure(self, approval_id: str, instance_id: str) -> ApprovalRecord:
        await self.open()
        existing = self._approvals.get(approval_id)
        if existing is not None:
            if existing.instance_id != instance_id:
                raise DurableConflict("control.approval_instance_conflict", approval_id)
            return existing
        created_at = datetime.now(UTC)
        await self._log.append(
            self._STREAM,
            f"approval-created:{approval_id}",
            "approval.created",
            {
                "approval_id": approval_id,
                "instance_id": instance_id,
                "created_at": created_at.isoformat(),
            },
        )
        record = ApprovalRecord(
            approval_id=approval_id,
            instance_id=instance_id,
            status="pending",
            decision=None,
            reason=None,
            created_at=created_at,
        )
        self._approvals[approval_id] = record
        return record

    async def get(self, approval_id: str) -> ApprovalRecord:
        await self.open()
        try:
            return self._approvals[approval_id]
        except KeyError as exc:
            raise DurableConflict("control.approval_not_found", approval_id) from exc

    async def list(self) -> tuple[ApprovalRecord, ...]:
        await self.open()
        return tuple(sorted(self._approvals.values(), key=lambda item: item.approval_id))

    async def decide(
        self,
        approval_id: str,
        decision: ApprovalDecisionSubmission,
    ) -> ApprovalRecord:
        current = await self.get(approval_id)
        if current.status == "decided":
            if current.decision == decision.decision and current.reason == decision.reason:
                return current
            raise DurableConflict("control.approval_already_decided", approval_id)
        decided_at = datetime.now(UTC)
        await self._log.append(
            self._STREAM,
            f"approval-decided:{approval_id}",
            "approval.decided",
            {
                "approval_id": approval_id,
                "decision": decision.decision,
                "reason": decision.reason,
                "decided_at": decided_at.isoformat(),
            },
        )
        updated = ApprovalRecord(
            approval_id=current.approval_id,
            instance_id=current.instance_id,
            status="decided",
            decision=decision.decision,
            reason=decision.reason,
            created_at=current.created_at,
            decided_at=decided_at,
        )
        self._approvals[approval_id] = updated
        return updated
