from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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


class WorkingDirectoryPolicy:
    """Resolve caller paths and optionally constrain them to configured roots."""

    def __init__(self, allowed_roots: Iterable[str | Path] = ()) -> None:
        self._allowed_roots = tuple(self._resolve_root(root) for root in allowed_roots)

    def resolve(self, cwd: str) -> Path:
        if not cwd.strip():
            raise ValueError("cwd must not be empty")
        requested = Path(cwd).expanduser()
        if not requested.is_absolute():
            raise ValueError("cwd must be an absolute path")
        try:
            resolved = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"cwd is unavailable: {cwd}") from exc
        if not resolved.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd}")
        if self._allowed_roots and not any(
            _is_within(resolved, root) for root in self._allowed_roots
        ):
            raise ValueError(f"cwd is rejected by the configured path filter: {cwd}")
        return resolved

    @staticmethod
    def _resolve_root(root: str | Path) -> Path:
        configured = Path(root).expanduser()
        if not configured.is_absolute():
            raise ValueError("allowed path roots must be absolute")
        try:
            resolved = configured.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"allowed path root is unavailable: {root}") from exc
        if not resolved.is_dir():
            raise ValueError(f"allowed path root is not a directory: {root}")
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
    cwd_policy: WorkingDirectoryPolicy,
) -> DelegationRequest:
    workspace = cwd_policy.resolve(submission.cwd)
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
                "cwd": str(workspace),
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


def delegation_continuation_input(
    snapshot: DelegationSnapshot,
    input_value: Mapping[str, object],
) -> JsonObject:
    result = dict(input_value)
    for field_name in ("cwd", "sandbox"):
        value = snapshot.request.input.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise DelegationCapabilityRejected(
                "delegation.gateway_context_missing",
                f"delegation is missing trusted Gateway field {field_name}",
            )
        provided = input_value.get(field_name)
        if provided is not None and provided != value:
            raise DelegationCapabilityRejected(
                "delegation.gateway_context_override",
                f"continuation cannot override Gateway field {field_name}",
            )
        result[field_name] = value
    return cast(JsonObject, result)


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
    cwd = metadata.get("cwd")
    plan_hash = metadata.get("plan_hash")
    policy_context = metadata.get("policy_context")
    if not isinstance(cwd, str) or not cwd.strip():
        raise DelegationCapabilityRejected(
            "delegation.cwd_required",
            "decision-bound delegation is missing its trusted working directory",
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
            "cwd": cwd,
        },
        policy_snapshot={
            "delegation_request_fingerprint": delegation_request_fingerprint(request),
            "cwd": cwd,
            "provider_id": request.provider_id,
            "model": request.model,
            "effort": request.effort,
            "policy_context": cast(JsonObject, policy_context),
            "delegation_policy": _delegation_policy_snapshot(request),
        },
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


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
