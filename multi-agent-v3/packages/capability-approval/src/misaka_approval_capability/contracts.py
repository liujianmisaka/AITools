from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from misaka_interaction_contracts import (
    DecisionFact,
    DecisionProposal,
    DecisionRef,
    DecisionStatus,
    PrincipalRef,
)


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    proposal: DecisionProposal
    fact: DecisionFact | None = None

    def __post_init__(self) -> None:
        if self.fact is not None and not self.fact.matches(self.proposal):
            raise ValueError("decision fact does not bind the stored proposal")

    @property
    def status(self) -> DecisionStatus:
        return self.fact.status if self.fact is not None else DecisionStatus.PENDING


class DecisionStore(Protocol):
    async def ensure(self, proposal: DecisionProposal) -> DecisionRecord: ...

    async def get(self, ref: DecisionRef) -> DecisionRecord: ...

    async def latest(self, proposal_id: str) -> DecisionRecord: ...

    async def list(self) -> tuple[DecisionRecord, ...]: ...

    async def decide(
        self,
        ref: DecisionRef,
        *,
        status: DecisionStatus,
        decided_by: PrincipalRef,
        reason: str = "",
    ) -> DecisionRecord: ...
