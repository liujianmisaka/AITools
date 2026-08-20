from __future__ import annotations

import asyncio

from misaka_interaction_contracts import (
    DecisionFact,
    DecisionProposal,
    DecisionRef,
    DecisionStatus,
    PrincipalRef,
    decision_fingerprint,
)

from misaka_approval_capability.contracts import DecisionRecord
from misaka_approval_capability.errors import DecisionConflict, DecisionNotFound


class MemoryDecisionStore:
    def __init__(self) -> None:
        self._decisions: dict[tuple[str, int], DecisionRecord] = {}
        self._lock = asyncio.Lock()

    async def ensure(self, proposal: DecisionProposal) -> DecisionRecord:
        async with self._lock:
            key = _key(proposal.ref)
            existing = self._decisions.get(key)
            if existing is not None:
                _require_same_proposal(existing.proposal, proposal)
                return existing
            record = DecisionRecord(proposal)
            self._decisions[key] = record
            return record

    async def get(self, ref: DecisionRef) -> DecisionRecord:
        async with self._lock:
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
            updated = DecisionRecord(current.proposal, fact)
            self._decisions[_key(ref)] = updated
            return updated


def _key(ref: DecisionRef) -> tuple[str, int]:
    return ref.proposal_id, ref.revision


def _require_same_proposal(
    existing: DecisionProposal,
    candidate: DecisionProposal,
) -> None:
    if decision_fingerprint(existing) != decision_fingerprint(candidate):
        raise DecisionConflict(
            "decision.proposal_conflict",
            (
                f"decision {candidate.ref.proposal_id} revision {candidate.ref.revision} "
                "was reused for a different plan or effect scope"
            ),
        )
