from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from misaka_approval_capability import (
    DecisionConflict,
    DecisionDenied,
    DecisionNotFound,
    DecisionRequired,
    DecisionStore,
)
from misaka_approval_capability import (
    DecisionGate as ApprovalDecisionGate,
)
from misaka_delegation_capability import DelegationCapabilityRejected
from misaka_delegation_contracts import (
    DelegationAdmission,
    DelegationBudget,
    DelegationPolicy,
    DelegationRequest,
    DelegationSnapshot,
    delegation_request_fingerprint,
)
from misaka_interaction_contracts import DecisionProposal, DecisionRef, PrincipalRef, ScopeRef
from misaka_kernel_contracts import JsonObject, JsonValue

from misaka_control_plane.models import DelegationSubmission, PrincipalSubmission, ScopeSubmission

_GATEWAY_METADATA_KEY = "_misaka_gateway"
_PLAN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class WorkspaceCatalog:
    """Resolve opaque workspace identifiers through an application-owned allowlist."""

    def __init__(self, entries: Mapping[str, str | Path] | None = None) -> None:
        self._entries: dict[str, Path] = {}
        for workspace_id, path in (entries or {}).items():
            if not workspace_id.strip():
                raise ValueError("workspace id must not be empty")
            self._entries[workspace_id] = Path(path)

    def resolve(self, workspace_id: str) -> Path:
        if not workspace_id.strip():
            raise ValueError("workspace id must not be empty")
        configured = self._entries.get(workspace_id)
        if configured is None:
            raise ValueError(f"workspace {workspace_id} is not registered")
        try:
            resolved = configured.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"workspace {workspace_id} is unavailable") from exc
        if not resolved.is_dir():
            raise ValueError(f"workspace {workspace_id} is not a directory")
        return resolved


class DelegationDecisionGate:
    """Bind Delegation admission to the shared durable Decision capability."""

    def __init__(self, store: DecisionStore) -> None:
        self._gate = ApprovalDecisionGate(store)

    async def authorize(
        self,
        request: DelegationRequest,
        parent: DelegationSnapshot | None,
    ) -> DelegationAdmission:
        del parent
        if not request.policy.require_decision and request.decision_ref is None:
            return DelegationAdmission(
                allowed=True,
                reason="delegation admitted by Control Plane policy",
                policy_snapshot=_delegation_policy_snapshot(request),
            )
        if request.decision_ref is None:
            raise DecisionRequired(
                "delegation.decision_ref_required",
                "a delegation requiring approval must declare decision_ref",
            )
        proposal = _delegation_decision_proposal(request)
        fact = await self._gate.authorize(proposal)
        return DelegationAdmission(
            allowed=True,
            reason="delegation approved by durable Decision fact",
            decision_ref=fact.ref,
            policy_snapshot=proposal.policy_snapshot,
        )

    async def evaluate(
        self,
        request: DelegationRequest,
        parent: DelegationSnapshot | None,
    ) -> DelegationAdmission:
        try:
            return await self.authorize(request, parent)
        except (
            DecisionConflict,
            DecisionDenied,
            DecisionNotFound,
            DecisionRequired,
            DelegationCapabilityRejected,
        ) as exc:
            return DelegationAdmission(
                allowed=False,
                reason=str(exc),
                decision_ref=request.decision_ref,
                policy_snapshot=_delegation_policy_snapshot(request),
                error_code=exc.code,
            )


def delegation_request_from_submission(
    submission: DelegationSubmission,
    workspace_catalog: WorkspaceCatalog,
) -> DelegationRequest:
    workspace = workspace_catalog.resolve(submission.workspace_id)
    policy_context = cast(JsonObject, submission.policy_context)
    request_input = cast(
        JsonObject,
        {
            **submission.input,
            "cwd": str(workspace),
            "sandbox": policy_context["sandbox"],
        },
    )
    constraints = cast(
        JsonObject,
        {
            "network_policy": policy_context["network_policy"],
            _GATEWAY_METADATA_KEY: {
                "workspace_id": submission.workspace_id,
                "plan_hash": submission.plan_hash,
                "policy_context": policy_context,
            },
        },
    )
    return DelegationRequest(
        delegation_id=submission.delegation_id,
        idempotency_key=submission.idempotency_key,
        initiator=_principal(submission.initiator),
        controller=_principal(submission.controller),
        scope=_scope(submission.scope),
        capability_id=submission.capability_id,
        operation=submission.operation,
        input=request_input,
        provider_id=submission.provider_id,
        model=submission.model,
        effort=submission.effort,
        output_schema=cast(JsonObject | None, submission.output_schema),
        mode=submission.mode,
        parent_delegation_id=submission.parent_delegation_id,
        session_id=submission.session_id,
        channel_id=submission.channel_id,
        decision_ref=(
            DecisionRef(
                submission.decision_ref.proposal_id,
                submission.decision_ref.revision,
            )
            if submission.decision_ref is not None
            else None
        ),
        required_features=frozenset(submission.required_features),
        constraints=constraints,
        observers=tuple(_principal(observer) for observer in submission.observers),
        policy=DelegationPolicy(
            child_scope=(
                _scope(submission.policy.child_scope)
                if submission.policy.child_scope is not None
                else None
            ),
            budget=DelegationBudget(
                max_depth=submission.policy.budget.max_depth,
                fan_out_limit=submission.policy.budget.fan_out_limit,
                max_concurrent_children=submission.policy.budget.max_concurrent_children,
                max_activations=submission.policy.budget.max_activations,
                time_budget_seconds=submission.policy.budget.time_budget_seconds,
                resource_budget=cast(
                    JsonObject,
                    submission.policy.budget.resource_budget,
                ),
            ),
            tool_allowlist=frozenset(submission.policy.tool_allowlist),
            tool_denylist=frozenset(submission.policy.tool_denylist),
            persona=submission.policy.persona,
            requested_effects=tuple(submission.policy.requested_effects),
            require_decision=submission.policy.require_decision,
        ),
    )


def _delegation_decision_proposal(request: DelegationRequest) -> DecisionProposal:
    if request.decision_ref is None:
        raise DecisionRequired(
            "delegation.decision_ref_required",
            "a delegation requiring approval must declare decision_ref",
        )
    metadata_value = request.constraints.get(_GATEWAY_METADATA_KEY)
    if not isinstance(metadata_value, dict):
        raise DelegationCapabilityRejected(
            "delegation.gateway_metadata_required",
            "decision-bound delegation is missing trusted Gateway metadata",
        )
    metadata = cast(dict[str, JsonValue], metadata_value)
    workspace_id = metadata.get("workspace_id")
    plan_hash = metadata.get("plan_hash")
    policy_context = metadata.get("policy_context")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise DelegationCapabilityRejected(
            "delegation.workspace_id_required",
            "decision-bound delegation is missing workspace identity",
        )
    if not isinstance(plan_hash, str) or _PLAN_HASH_PATTERN.fullmatch(plan_hash) is None:
        raise DelegationCapabilityRejected(
            "delegation.plan_hash_invalid",
            "decision-bound delegation has an invalid plan hash",
        )
    if not isinstance(policy_context, dict):
        raise DelegationCapabilityRejected(
            "delegation.policy_context_required",
            "decision-bound delegation is missing trusted policy context",
        )
    return DecisionProposal(
        ref=request.decision_ref,
        plan_hash=plan_hash,
        requested_effects=request.policy.requested_effects,
        scope=request.scope,
        created_by=request.initiator,
        payload={
            "delegation_id": request.delegation_id,
            "workspace_id": workspace_id,
        },
        policy_snapshot={
            "delegation_request_fingerprint": delegation_request_fingerprint(request),
            "workspace_id": workspace_id,
            "provider_id": request.provider_id,
            "model": request.model,
            "effort": request.effort,
            "policy_context": cast(JsonObject, policy_context),
            "delegation_policy": _delegation_policy_snapshot(request),
        },
    )


def _delegation_policy_snapshot(request: DelegationRequest) -> JsonObject:
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


def _principal(value: PrincipalSubmission) -> PrincipalRef:
    return PrincipalRef(value.principal_id, value.kind, value.display_name)


def _scope(value: ScopeSubmission) -> ScopeRef:
    return ScopeRef(value.scope_id, value.parent_scope_id)
