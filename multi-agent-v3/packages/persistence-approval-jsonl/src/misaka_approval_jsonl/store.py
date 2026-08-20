from __future__ import annotations

import asyncio
from datetime import datetime
from typing import cast

from misaka_approval_capability import (
    DecisionConflict,
    DecisionNotFound,
    DecisionRecord,
)
from misaka_interaction_contracts import (
    DecisionFact,
    DecisionProposal,
    DecisionRef,
    DecisionStatus,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
    decision_fingerprint,
)
from misaka_kernel_contracts import JsonObject
from misaka_persistence_jsonl import JsonlEventLog


class JsonlDecisionStore:
    _STREAM = "capability.decisions"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._decisions: dict[tuple[str, int], DecisionRecord] = {}
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
            if event.event_type == "decision.proposed":
                proposal = _decode_proposal(payload)
                key = _key(proposal.ref)
                if key in self._decisions:
                    raise DecisionConflict(
                        "decision.history_invalid",
                        f"decision {proposal.ref.proposal_id} revision {proposal.ref.revision} "
                        "has duplicate proposal facts",
                    )
                self._decisions[key] = DecisionRecord(proposal)
            elif event.event_type == "decision.decided":
                fact = _decode_fact(payload)
                key = _key(fact.ref)
                current = self._decisions.get(key)
                if (
                    current is None
                    or current.fact is not None
                    or not fact.matches(current.proposal)
                ):
                    raise DecisionConflict(
                        "decision.history_invalid",
                        f"decision {fact.ref.proposal_id} revision {fact.ref.revision} "
                        "contains an invalid decision fact",
                    )
                self._decisions[key] = DecisionRecord(current.proposal, fact)
            else:
                raise DecisionConflict(
                    "decision.event_unknown",
                    f"unknown decision event {event.event_type}",
                )
        self._loaded = True

    async def ensure(self, proposal: DecisionProposal) -> DecisionRecord:
        async with self._lock:
            await self._open_unlocked()
            key = _key(proposal.ref)
            existing = self._decisions.get(key)
            if existing is not None:
                if decision_fingerprint(existing.proposal) != decision_fingerprint(proposal):
                    raise DecisionConflict(
                        "decision.proposal_conflict",
                        (
                            f"decision {proposal.ref.proposal_id} revision "
                            f"{proposal.ref.revision} was reused for a different plan"
                        ),
                    )
                return existing
            await self._log.append(
                self._STREAM,
                f"decision-proposed:{proposal.ref.proposal_id}:{proposal.ref.revision}",
                "decision.proposed",
                _encode_proposal(proposal),
            )
            record = DecisionRecord(proposal)
            self._decisions[key] = record
            return record

    async def get(self, ref: DecisionRef) -> DecisionRecord:
        async with self._lock:
            await self._open_unlocked()
            try:
                return self._decisions[_key(ref)]
            except KeyError as exc:
                raise DecisionNotFound(
                    "decision.not_found",
                    f"decision {ref.proposal_id} revision {ref.revision} was not found",
                ) from exc

    async def latest(self, proposal_id: str) -> DecisionRecord:
        if not proposal_id.strip():
            raise ValueError("proposal id must not be empty")
        async with self._lock:
            await self._open_unlocked()
            matches = [
                record
                for (stored_id, _), record in self._decisions.items()
                if stored_id == proposal_id
            ]
            if not matches:
                raise DecisionNotFound(
                    "decision.not_found",
                    f"decision {proposal_id} was not found",
                )
            return max(matches, key=lambda record: record.proposal.ref.revision)

    async def list(self) -> tuple[DecisionRecord, ...]:
        async with self._lock:
            await self._open_unlocked()
            return tuple(
                sorted(
                    self._decisions.values(),
                    key=lambda item: (
                        item.proposal.ref.proposal_id,
                        item.proposal.ref.revision,
                    ),
                )
            )

    async def decide(
        self,
        ref: DecisionRef,
        *,
        status: DecisionStatus,
        decided_by: PrincipalRef,
        reason: str = "",
    ) -> DecisionRecord:
        if status is DecisionStatus.PENDING:
            raise ValueError("a decision fact must be terminal")
        async with self._lock:
            await self._open_unlocked()
            current = self._decisions.get(_key(ref))
            if current is None:
                raise DecisionNotFound(
                    "decision.not_found",
                    f"decision {ref.proposal_id} revision {ref.revision} was not found",
                )
            fact = DecisionFact.from_proposal(
                current.proposal,
                status=status,
                decided_by=decided_by,
                reason=reason,
            )
            if current.fact is not None:
                if (
                    current.fact.status is fact.status
                    and current.fact.decided_by == fact.decided_by
                    and current.fact.reason == fact.reason
                ):
                    return current
                raise DecisionConflict(
                    "decision.already_decided",
                    f"decision {ref.proposal_id} revision {ref.revision} is already terminal",
                )
            await self._log.append(
                self._STREAM,
                f"decision-decided:{ref.proposal_id}:{ref.revision}",
                "decision.decided",
                _encode_fact(fact),
            )
            updated = DecisionRecord(current.proposal, fact)
            self._decisions[_key(ref)] = updated
            return updated


def _key(ref: DecisionRef) -> tuple[str, int]:
    return ref.proposal_id, ref.revision


def _encode_principal(principal: PrincipalRef) -> JsonObject:
    return {
        "principal_id": principal.principal_id,
        "kind": principal.kind.value,
        "display_name": principal.display_name,
    }


def _decode_principal(payload: object) -> PrincipalRef:
    if not isinstance(payload, dict):
        raise TypeError("decision principal must be an object")
    values = cast(dict[str, object], payload)
    return PrincipalRef(
        principal_id=str(values["principal_id"]),
        kind=PrincipalKind(str(values["kind"])),
        display_name=str(values.get("display_name", "")),
    )


def _encode_scope(scope: ScopeRef) -> JsonObject:
    return {
        "scope_id": scope.scope_id,
        "parent_scope_id": scope.parent_scope_id,
    }


def _decode_scope(payload: object) -> ScopeRef:
    if not isinstance(payload, dict):
        raise TypeError("decision scope must be an object")
    values = cast(dict[str, object], payload)
    parent = values.get("parent_scope_id")
    return ScopeRef(
        scope_id=str(values["scope_id"]),
        parent_scope_id=str(parent) if parent is not None else None,
    )


def _encode_proposal(proposal: DecisionProposal) -> JsonObject:
    return {
        "proposal_id": proposal.ref.proposal_id,
        "revision": proposal.ref.revision,
        "plan_hash": proposal.plan_hash,
        "requested_effects": list(proposal.requested_effects),
        "scope": _encode_scope(proposal.scope),
        "created_by": _encode_principal(proposal.created_by),
        "payload": proposal.payload,
        "policy_snapshot": proposal.policy_snapshot,
        "created_at": proposal.created_at.isoformat(),
    }


def _decode_proposal(payload: JsonObject) -> DecisionProposal:
    effects = payload["requested_effects"]
    if not isinstance(effects, list) or not all(isinstance(item, str) for item in effects):
        raise TypeError("decision effects must be a string array")
    raw_payload = payload.get("payload", {})
    policy_snapshot = payload.get("policy_snapshot", {})
    if not isinstance(raw_payload, dict) or not isinstance(policy_snapshot, dict):
        raise TypeError("decision payloads must be objects")
    return DecisionProposal(
        ref=DecisionRef(str(payload["proposal_id"]), int(str(payload["revision"]))),
        plan_hash=str(payload["plan_hash"]),
        requested_effects=tuple(cast(list[str], effects)),
        scope=_decode_scope(payload["scope"]),
        created_by=_decode_principal(payload["created_by"]),
        payload=cast(JsonObject, raw_payload),
        policy_snapshot=cast(JsonObject, policy_snapshot),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
    )


def _encode_fact(fact: DecisionFact) -> JsonObject:
    return {
        "proposal_id": fact.ref.proposal_id,
        "revision": fact.ref.revision,
        "status": fact.status.value,
        "plan_hash": fact.plan_hash,
        "requested_effects": list(fact.requested_effects),
        "scope": _encode_scope(fact.scope),
        "policy_snapshot": fact.policy_snapshot,
        "decided_by": _encode_principal(fact.decided_by),
        "reason": fact.reason,
        "decided_at": fact.decided_at.isoformat(),
    }


def _decode_fact(payload: JsonObject) -> DecisionFact:
    effects = payload["requested_effects"]
    policy_snapshot = payload.get("policy_snapshot", {})
    if not isinstance(effects, list) or not all(isinstance(item, str) for item in effects):
        raise TypeError("decision effects must be a string array")
    if not isinstance(policy_snapshot, dict):
        raise TypeError("decision policy snapshot must be an object")
    return DecisionFact(
        ref=DecisionRef(str(payload["proposal_id"]), int(str(payload["revision"]))),
        status=DecisionStatus(str(payload["status"])),
        plan_hash=str(payload["plan_hash"]),
        requested_effects=tuple(cast(list[str], effects)),
        scope=_decode_scope(payload["scope"]),
        policy_snapshot=cast(JsonObject, policy_snapshot),
        decided_by=_decode_principal(payload["decided_by"]),
        reason=str(payload.get("reason", "")),
        decided_at=datetime.fromisoformat(str(payload["decided_at"])),
    )
