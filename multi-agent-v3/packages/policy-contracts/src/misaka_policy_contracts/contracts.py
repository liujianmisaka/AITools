from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from misaka_interaction_contracts import DecisionProposal
from misaka_kernel_contracts import ContractError, JsonObject


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_DECISION = "require_decision"


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    proposal: DecisionProposal
    context: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: PolicyEffect
    reason: str
    constraints: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ContractError("policy.reason_empty", "policy reason must not be empty")


class PolicyProvider(Protocol):
    async def evaluate(self, request: PolicyRequest) -> PolicyDecision: ...
