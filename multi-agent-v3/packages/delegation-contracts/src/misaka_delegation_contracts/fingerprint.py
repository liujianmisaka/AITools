from __future__ import annotations

import hashlib
import json

from misaka_interaction_contracts import PrincipalRef, ScopeRef
from misaka_kernel_contracts import JsonObject, JsonValue

from misaka_delegation_contracts.contracts import DelegationRequest


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
