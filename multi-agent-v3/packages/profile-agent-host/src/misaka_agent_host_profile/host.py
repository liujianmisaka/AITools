from __future__ import annotations

from types import TracebackType
from typing import cast

from misaka_agent_capability import AGENT_PROVIDER_SERVICE
from misaka_fake_agent import (
    FAKE_AGENT_MODULE_ID,
    FakeAgentModule,
    FakeAgentScenario,
)
from misaka_invocation_contracts import InvocationRequest
from misaka_invocation_runtime import (
    INVOCATION_RUNTIME_MODULE_ID,
    INVOCATION_RUNTIME_SERVICE,
    InvocationRuntime,
    InvocationRuntimeModule,
    RuntimeInvocationHandle,
)
from misaka_kernel import Host, HostStatus, ProfileDefinition, ProfileLoader


class AgentHost:
    def __init__(self, host: Host, *, provider_id: str) -> None:
        self._host = host
        self.provider_id = provider_id

    @property
    def status(self) -> HostStatus:
        return self._host.status

    async def start(self) -> None:
        await self._host.start()

    async def submit(self, request: InvocationRequest) -> RuntimeInvocationHandle:
        if self._host.status is not HostStatus.ACTIVE:
            raise RuntimeError("agent host must be active before submitting invocations")
        runtime = cast(
            InvocationRuntime,
            self._host.services.require(INVOCATION_RUNTIME_SERVICE),
        )
        return await runtime.submit(request, provider_id=self.provider_id)

    async def stop(self) -> None:
        await self._host.stop()

    async def __aenter__(self) -> AgentHost:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.stop()


def create_fake_agent_host(
    scenario: FakeAgentScenario | None = None,
    *,
    provider_id: str = "fake-agent",
) -> AgentHost:
    runtime_module = InvocationRuntimeModule()
    fake_module = FakeAgentModule(scenario, provider_id=provider_id)
    loader = ProfileLoader(
        {
            INVOCATION_RUNTIME_MODULE_ID: lambda: runtime_module,
            FAKE_AGENT_MODULE_ID: lambda: fake_module,
        }
    )
    profile = ProfileDefinition(
        profile_id="agent-host",
        module_ids=(INVOCATION_RUNTIME_MODULE_ID, FAKE_AGENT_MODULE_ID),
        bindings={AGENT_PROVIDER_SERVICE: provider_id},
    )
    return AgentHost(loader.create_host(profile), provider_id=provider_id)
