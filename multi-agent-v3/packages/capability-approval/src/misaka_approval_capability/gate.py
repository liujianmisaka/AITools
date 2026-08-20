from __future__ import annotations

from misaka_interaction_contracts import DecisionFact, DecisionProposal, DecisionStatus

from misaka_approval_capability.contracts import DecisionStore
from misaka_approval_capability.errors import DecisionDenied, DecisionRequired


class DecisionGate:
    def __init__(self, store: DecisionStore) -> None:
        self.store = store

    async def authorize(self, proposal: DecisionProposal) -> DecisionFact:
        record = await self.store.ensure(proposal)
        fact = record.fact
        if fact is None:
            raise DecisionRequired(
                "decision.required",
                (
                    f"decision {proposal.ref.proposal_id} revision "
                    f"{proposal.ref.revision} is pending"
                ),
            )
        if not fact.matches(proposal):
            raise DecisionDenied(
                "decision.binding_mismatch",
                "decision fact does not authorize this plan revision and effect scope",
            )
        if fact.status is DecisionStatus.APPROVED:
            return fact
        raise DecisionDenied(
            f"decision.{fact.status.value}",
            fact.reason or f"decision is {fact.status.value}",
        )
