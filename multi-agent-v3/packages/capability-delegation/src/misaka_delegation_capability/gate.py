from __future__ import annotations

from typing import Protocol

from misaka_delegation_contracts import (
    DelegationAdmission,
    DelegationRequest,
    DelegationSnapshot,
)
from misaka_kernel_contracts import JsonObject, JsonValue


class DelegationGate(Protocol):
    """Decides whether a Delegation may publish an external Activation."""

    async def evaluate(
        self,
        request: DelegationRequest,
        parent: DelegationSnapshot | None,
    ) -> DelegationAdmission: ...


class AllowAllDelegationGate:
    """A deliberately explicit local default for profiles without approvals."""

    async def evaluate(
        self,
        request: DelegationRequest,
        parent: DelegationSnapshot | None,
    ) -> DelegationAdmission:
        del parent
        if request.policy.require_decision:
            return DelegationAdmission(
                allowed=False,
                reason="delegation requires a Decision Gate",
                error_code="delegation.decision_required",
            )
        if request.decision_ref is not None:
            return DelegationAdmission(
                allowed=False,
                reason="no Decision Gate is bound to this profile",
                error_code="delegation.decision_gate_unavailable",
            )
        return DelegationAdmission(
            allowed=True,
            reason="delegation admitted by local profile",
            policy_snapshot=_policy_snapshot(request),
        )


class StaticDelegationGate:
    """Deterministic gate used by local profiles and contract tests."""

    def __init__(self, admission: DelegationAdmission) -> None:
        self.admission = admission
        self.evaluations = 0

    async def evaluate(
        self,
        request: DelegationRequest,
        parent: DelegationSnapshot | None,
    ) -> DelegationAdmission:
        del request, parent
        self.evaluations += 1
        return self.admission


def _policy_snapshot(request: DelegationRequest) -> JsonObject:
    policy = request.policy
    return {
        "child_scope": (
            {
                "scope_id": policy.child_scope.scope_id,
                "parent_scope_id": policy.child_scope.parent_scope_id,
            }
            if policy.child_scope is not None
            else None
        ),
        "budget": {
            "max_depth": policy.budget.max_depth,
            "fan_out_limit": policy.budget.fan_out_limit,
            "max_concurrent_children": policy.budget.max_concurrent_children,
            "max_activations": policy.budget.max_activations,
            "time_budget_seconds": policy.budget.time_budget_seconds,
            "resource_budget": policy.budget.resource_budget,
        },
        "tool_allowlist": list[JsonValue](sorted(policy.tool_allowlist)),
        "tool_denylist": list[JsonValue](sorted(policy.tool_denylist)),
        "persona": policy.persona,
        "requested_effects": list[JsonValue](policy.requested_effects),
        "require_decision": policy.require_decision,
    }
