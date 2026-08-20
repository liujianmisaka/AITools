from __future__ import annotations

from types import TracebackType

from misaka_agent_capability import AGENT_PROVIDER_SERVICE
from misaka_approval_capability import DecisionGate
from misaka_artifact_capability import ARTIFACT_MODULE_ID, MemoryArtifactStoreModule
from misaka_fake_agent import (
    FAKE_AGENT_MODULE_ID,
    FakeAgentModule,
    FakeAgentScenario,
)
from misaka_invocation_contracts import InvocationRequest
from misaka_invocation_runtime import (
    INVOCATION_RUNTIME_MODULE_ID,
    InvocationRuntime,
    InvocationRuntimeModule,
    RuntimeInvocationHandle,
)
from misaka_kernel import Host, HostStatus, ProfileDefinition, ProfileLoader
from misaka_policy_capability import (
    POLICY_MODULE_ID,
    PolicyModule,
    StaticPolicyProvider,
)
from misaka_policy_contracts import PolicyDecision, PolicyEffect, PolicyProvider
from misaka_process_capability import FAKE_PROCESS_MODULE_ID, FakeProcessModule
from misaka_session_capability import MEMORY_SESSION_MODULE_ID, MemorySessionStoreModule
from misaka_workspace_capability import FAKE_WORKSPACE_MODULE_ID, FakeWorkspaceModule


class AgentHost:
    def __init__(
        self,
        host: Host,
        *,
        provider_id: str,
        runtime: InvocationRuntime,
    ) -> None:
        self._host = host
        self.provider_id = provider_id
        self._runtime = runtime

    @property
    def status(self) -> HostStatus:
        return self._host.status

    @property
    def runtime(self) -> InvocationRuntime:
        return self._runtime

    async def start(self) -> None:
        await self._host.start()

    async def submit(self, request: InvocationRequest) -> RuntimeInvocationHandle:
        if self._host.status is not HostStatus.ACTIVE:
            raise RuntimeError("agent host must be active before submitting invocations")
        return await self.runtime.submit(request, provider_id=self.provider_id)

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
    policy_provider: PolicyProvider | None = None,
    decision_gate: DecisionGate | None = None,
) -> AgentHost:
    runtime_module = InvocationRuntimeModule()
    policy_module = PolicyModule(
        policy_provider
        or StaticPolicyProvider(
            PolicyDecision(
                PolicyEffect.ALLOW,
                "trusted local fake-agent profile",
            )
        ),
        decision_gate=decision_gate,
    )
    artifact_module = MemoryArtifactStoreModule()
    session_module = MemorySessionStoreModule()
    process_module = FakeProcessModule()
    workspace_module = FakeWorkspaceModule()
    fake_module = FakeAgentModule(scenario, provider_id=provider_id)
    loader = ProfileLoader(
        {
            INVOCATION_RUNTIME_MODULE_ID: lambda: runtime_module,
            POLICY_MODULE_ID: lambda: policy_module,
            ARTIFACT_MODULE_ID: lambda: artifact_module,
            MEMORY_SESSION_MODULE_ID: lambda: session_module,
            FAKE_PROCESS_MODULE_ID: lambda: process_module,
            FAKE_WORKSPACE_MODULE_ID: lambda: workspace_module,
            FAKE_AGENT_MODULE_ID: lambda: fake_module,
        }
    )
    profile = ProfileDefinition(
        profile_id="agent-host",
        module_ids=(
            INVOCATION_RUNTIME_MODULE_ID,
            POLICY_MODULE_ID,
            ARTIFACT_MODULE_ID,
            MEMORY_SESSION_MODULE_ID,
            FAKE_PROCESS_MODULE_ID,
            FAKE_WORKSPACE_MODULE_ID,
            FAKE_AGENT_MODULE_ID,
        ),
        bindings={AGENT_PROVIDER_SERVICE: provider_id},
    )
    return AgentHost(
        loader.create_host(profile),
        provider_id=provider_id,
        runtime=runtime_module.runtime,
    )
