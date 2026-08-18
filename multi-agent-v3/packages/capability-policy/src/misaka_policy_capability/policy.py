from __future__ import annotations

from typing import Protocol, cast

from misaka_invocation_contracts import (
    InvocationRequest,
    PolicyDecision,
    PolicyEffect,
)
from misaka_invocation_runtime import (
    INVOCATION_RUNTIME_SERVICE,
    InvocationRejected,
    InvocationRuntime,
)
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    ModuleId,
    ModuleManifest,
    ServiceKey,
    ServiceProvision,
    ServiceRequirement,
)

POLICY_PROVIDER_SERVICE = ServiceKey("capability.policy.provider")
POLICY_MODULE_ID = ModuleId("capability.policy.static")


class PolicyProvider(Protocol):
    async def evaluate(self, request: InvocationRequest) -> PolicyDecision: ...


class StaticPolicyProvider:
    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        self.evaluations = 0

    async def evaluate(self, request: InvocationRequest) -> PolicyDecision:
        del request
        self.evaluations += 1
        return self.decision


class PolicyGuard:
    def __init__(self, provider: PolicyProvider) -> None:
        self.provider = provider

    async def check(self, request: InvocationRequest) -> None:
        decision = await self.provider.evaluate(request)
        if decision.effect is PolicyEffect.ALLOW:
            return
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            raise InvocationRejected(
                "policy.approval_required",
                decision.reason,
            )
        raise InvocationRejected("policy.denied", decision.reason)


class PolicyModule:
    def __init__(self, provider: PolicyProvider) -> None:
        self.provider = provider
        self.guard = PolicyGuard(provider)

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
