from __future__ import annotations

from typing import cast

from misaka_agent_capability import AGENT_PROVIDER_SERVICE
from misaka_invocation_runtime import (
    INVOCATION_RUNTIME_SERVICE,
    InvocationRuntime,
)
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    ModuleId,
    ModuleManifest,
    ServiceProvision,
    ServiceRequirement,
    ServiceShape,
)

from misaka_fake_agent.provider import FakeAgentProvider, FakeAgentScenario

FAKE_AGENT_MODULE_ID = ModuleId("provider.agent.fake")


class FakeAgentModule:
    def __init__(
        self,
        scenario: FakeAgentScenario | None = None,
        *,
        provider_id: str = "fake-agent",
    ) -> None:
        if not provider_id.strip():
            raise ValueError("provider_id must not be empty")
        self.provider_id = provider_id
        self.provider = FakeAgentProvider(scenario)

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=FAKE_AGENT_MODULE_ID,
            version="1.0.0",
            requires=(ServiceRequirement(INVOCATION_RUNTIME_SERVICE, version="1.0.0"),),
            provides=(
                ServiceProvision(
                    AGENT_PROVIDER_SERVICE,
                    "1.0.0",
                    shape=ServiceShape.NAMED,
                    name=self.provider_id,
                ),
            ),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        runtime = cast(InvocationRuntime, context.require(INVOCATION_RUNTIME_SERVICE))
        await runtime.register_provider(self.provider_id, self.provider)
        context.provide(
            AGENT_PROVIDER_SERVICE,
            self.provider,
            version="1.0.0",
            name=self.provider_id,
        )
        return None

    async def start(self, context: HostContext) -> None:
        del context
