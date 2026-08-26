from __future__ import annotations

import hashlib
import json

from misaka_interaction_contracts import PrincipalRef, ScopeRef
from misaka_kernel_contracts import JsonObject, JsonValue

from misaka_delegation_contracts.contracts import DelegationRequest
from misaka_delegation_contracts.dispatch import MessageDispatchRequest


def delegation_request_fingerprint(request: DelegationRequest) -> str:
    payload: JsonObject = {
        "delegation_id": request.delegation_id,
        "idempotency_key": request.idempotency_key,
        "initiator": _principal_payload(request.initiator),
        "controller": _principal_payload(request.controller),
        "scope": _scope_payload(request.scope),
        "capability_id": request.capability_id,
        "operation": request.operation,
        "input": request.input,
        "provider_id": request.provider_id,
        "model": request.model,
        "effort": request.effort,
        "output_schema": request.output_schema,
        "mode": request.mode.value,
        "parent_delegation_id": request.parent_delegation_id,
        "session_id": request.session_id,
        "channel_id": request.channel_id,
        "decision_ref": (
            {
                "proposal_id": request.decision_ref.proposal_id,
                "revision": request.decision_ref.revision,
            }
            if request.decision_ref is not None
            else None
        ),
        "required_features": list[JsonValue](sorted(request.required_features)),
        "constraints": request.constraints,
        "observers": list[JsonValue](
            [_principal_payload(observer) for observer in request.observers]
        ),
        "policy": _policy_payload(request),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def message_dispatch_request_fingerprint(request: MessageDispatchRequest) -> str:
    payload: JsonObject = {
        "dispatch_id": request.dispatch_id,
        "delegation_id": request.delegation_id,
        "idempotency_key": request.idempotency_key,
        "message_id": request.message_id,
        "actor": _principal_payload(request.actor),
        "session_id": request.session_id,
        "expected_activation_id": request.expected_activation_id,
        "delivery": request.delivery.value,
        "message_type": request.message_type.value,
        "payload": request.payload,
        "recipient": (
            _principal_payload(request.recipient) if request.recipient is not None else None
        ),
        "correlation_id": request.correlation_id,
        "causation_id": request.causation_id,
        "reply_to": request.reply_to,
        "model": request.model,
        "effort": request.effort,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _principal_payload(principal: PrincipalRef) -> JsonObject:
    return {
        "principal_id": principal.principal_id,
        "kind": principal.kind.value,
        "display_name": principal.display_name,
    }


def _scope_payload(scope: ScopeRef) -> JsonObject:
    return {"scope_id": scope.scope_id, "parent_scope_id": scope.parent_scope_id}


def _policy_payload(request: DelegationRequest) -> JsonObject:
    policy = request.policy
    return {
        "child_scope": (
            _scope_payload(policy.child_scope) if policy.child_scope is not None else None
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
