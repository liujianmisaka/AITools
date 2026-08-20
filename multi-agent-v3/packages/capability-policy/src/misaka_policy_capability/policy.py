from __future__ import annotations

from typing import cast

from misaka_approval_capability import DecisionDenied, DecisionGate, DecisionRequired
from misaka_interaction_contracts import (
    DecisionProposal,
    DecisionRef,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_invocation_contracts import InvocationRequest, request_fingerprint
from misaka_invocation_runtime import (
    INVOCATION_RUNTIME_SERVICE,
    InvocationRejected,
    InvocationRuntime,
)
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    JsonObject,
    JsonValue,
    ModuleId,
    ModuleManifest,
    ServiceKey,
    ServiceProvision,
    ServiceRequirement,
)
from misaka_policy_contracts import (
    PolicyDecision,
    PolicyEffect,
    PolicyProvider,
    PolicyRequest,
)

POLICY_PROVIDER_SERVICE = ServiceKey("capability.policy.provider")
POLICY_MODULE_ID = ModuleId("capability.policy.static")


class StaticPolicyProvider:
    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        self.evaluations = 0

    async def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        del request
        self.evaluations += 1
        return self.decision


class PolicyGuard:
    def __init__(
        self,
        provider: PolicyProvider,
        *,
        decision_gate: DecisionGate | None = None,
    ) -> None:
        self.provider = provider
        self.decision_gate = decision_gate

    async def check(self, request: InvocationRequest) -> None:
        proposal = invocation_decision_proposal(request)
        decision = await self.provider.evaluate(
            PolicyRequest(proposal, context=_public_policy_context(request.policy_context))
        )
        if decision.effect is PolicyEffect.ALLOW:
            return
        if decision.effect is PolicyEffect.DENY:
            raise InvocationRejected("policy.denied", decision.reason)
        if self.decision_gate is None:
            raise InvocationRejected(
                "policy.decision_gate_unavailable",
                "policy requires a decision but no decision gate is configured",
            )
        try:
            await self.decision_gate.authorize(proposal)
        except DecisionRequired as exc:
            raise InvocationRejected("policy.decision_required", str(exc)) from exc
        except DecisionDenied as exc:
            raise InvocationRejected("policy.decision_denied", str(exc)) from exc


class PolicyModule:
    def __init__(
        self,
        provider: PolicyProvider,
        *,
        decision_gate: DecisionGate | None = None,
    ) -> None:
        self.provider = provider
        self.guard = PolicyGuard(provider, decision_gate=decision_gate)

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=POLICY_MODULE_ID,
            version="1.0.0",
            requires=(ServiceRequirement(INVOCATION_RUNTIME_SERVICE, version="1.0.0"),),
            provides=(ServiceProvision(POLICY_PROVIDER_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer:
        runtime = cast(InvocationRuntime, context.require(INVOCATION_RUNTIME_SERVICE))
        remove_guard = runtime.add_guard(self.guard)
        context.provide(
            POLICY_PROVIDER_SERVICE,
            self.provider,
            version="1.0.0",
        )

        async def dispose() -> None:
            remove_guard()

        return dispose

    async def start(self, context: HostContext) -> None:
        del context


def invocation_decision_proposal(request: InvocationRequest) -> DecisionProposal:
    revision_value = request.policy_context.get("plan_revision", 1)
    if isinstance(revision_value, bool) or not isinstance(revision_value, int):
        raise InvocationRejected(
            "policy.plan_revision_invalid",
            "policy plan_revision must be a positive integer",
        )
    principal_id = _context_string(
        request.policy_context,
        "principal_id",
        "system:invocation-runtime",
    )
    scope_id = _context_string(
        request.policy_context,
        "scope_id",
        f"invocation:{request.invocation_id}",
    )
    return DecisionProposal(
        ref=DecisionRef(f"invocation:{request.invocation_id}", revision_value),
        plan_hash=request_fingerprint(request),
        requested_effects=(f"{request.capability_id}:{request.operation}",),
        scope=ScopeRef(scope_id),
        created_by=PrincipalRef(principal_id, PrincipalKind.APPLICATION),
        payload={
            "invocation_id": request.invocation_id,
            "capability_id": request.capability_id,
            "operation": request.operation,
        },
        policy_snapshot=_public_policy_context(request.policy_context),
    )


def _context_string(context: JsonObject, key: str, default: str) -> str:
    value = context.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise InvocationRejected(
            f"policy.{key}_invalid",
            f"policy {key} must be a non-empty string",
        )
    return value


def _public_policy_context(context: JsonObject) -> JsonObject:
    return {key: _redact_value(key, value) for key, value in context.items()}


def _redact_value(key: str, value: JsonValue) -> JsonValue:
    normalized = key.casefold()
    if any(marker in normalized for marker in ("secret", "token", "password", "api_key")):
        return "<redacted>"
    if isinstance(value, dict):
        return {nested: _redact_value(nested, item) for nested, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    return value
