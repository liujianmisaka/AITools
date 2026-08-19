from __future__ import annotations

import asyncio
from dataclasses import dataclass

from misaka_a2a_capability import A2AAgentCard, A2ASkill
from misaka_a2a_http import A2AHttpConfig, create_a2a_http_app
from misaka_a2a_runtime import A2AServer, A2AServerStatus, DelegationTaskHandler
from misaka_agent_capability import (
    AGENT_CAPABILITY_ID,
    AGENT_OPERATION_INVOKE,
    AGENT_PROVIDER_SERVICE,
)
from misaka_delegation_runtime import DelegationRuntime
from misaka_fake_agent import FAKE_AGENT_MODULE_ID, FakeAgentModule, FakeAgentScenario
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_invocation_contracts import CapabilityFeature
from misaka_invocation_runtime import (
    INVOCATION_RUNTIME_MODULE_ID,
    InvocationRuntime,
    InvocationRuntimeModule,
)
from misaka_kernel import Host, HostStatus, ProfileDefinition, ProfileLoader
from starlette.applications import Starlette


@dataclass(frozen=True, slots=True)
class A2ANodeConfig:
    host: str = "127.0.0.1"
    port: int = 8015
    public_url: str | None = None
    provider_id: str = "fake-agent"

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if self.host in {"0.0.0.0", "::"} and self.public_url is None:
            raise ValueError(
                "public_url is required when binding the A2A node to a wildcard address"
            )

    @property
    def advertised_url(self) -> str:
        return self.public_url or f"http://{self.host}:{self.port}"


class A2ANode:
    def __init__(
        self,
        host: Host,
        runtime: InvocationRuntime,
        delegation_runtime: DelegationRuntime,
        server: A2AServer,
        card: A2AAgentCard,
        config: A2ANodeConfig,
    ) -> None:
        self._host = host
        self.runtime = runtime
        self.delegation_runtime = delegation_runtime
        self.server = server
        self.card = card
        self.config = config
        self._lifecycle_lock = asyncio.Lock()
        self.app: Starlette = create_a2a_http_app(
            server,
            card,
            config=A2AHttpConfig(public_url=config.advertised_url),
            start=self.start,
            stop=self.stop,
        )

    @property
    def host_status(self) -> HostStatus:
        return self._host.status

    @property
    def server_status(self) -> A2AServerStatus:
        return self.server.status

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if (
                self._host.status is HostStatus.ACTIVE
                and self.server.status is A2AServerStatus.ACTIVE
            ):
                return
            try:
                await self._host.start()
                await self.server.start()
            except Exception:
                await self.server.stop()
                await self._host.stop()
                raise

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            try:
                await self.server.stop()
            finally:
                try:
                    await self.delegation_runtime.stop()
                finally:
                    await self._host.stop()


def create_fake_a2a_node(
    scenario: FakeAgentScenario | None = None,
    *,
    config: A2ANodeConfig | None = None,
) -> A2ANode:
    settings = config or A2ANodeConfig()
    runtime = InvocationRuntime(
        provider_start_timeout_seconds=15.0,
        cancellation_timeout_seconds=5.0,
        shutdown_timeout_seconds=10.0,
    )
    runtime_module = InvocationRuntimeModule(runtime)
    fake_module = FakeAgentModule(scenario, provider_id=settings.provider_id)
    loader = ProfileLoader(
        {
            INVOCATION_RUNTIME_MODULE_ID: lambda: runtime_module,
            FAKE_AGENT_MODULE_ID: lambda: fake_module,
        }
    )
    profile = ProfileDefinition(
        profile_id="a2a-node",
        module_ids=(INVOCATION_RUNTIME_MODULE_ID, FAKE_AGENT_MODULE_ID),
        bindings={AGENT_PROVIDER_SERVICE: settings.provider_id},
    )
    host = loader.create_host(profile)
    delegation_runtime = DelegationRuntime(
        runtime,
        MemoryInteractionChannelStore(),
    )
    features = frozenset(
        {
            CapabilityFeature.STRUCTURED_OUTPUT,
            CapabilityFeature.STREAMING,
            CapabilityFeature.CANCELLATION,
        }
    )
    card = A2AAgentCard(
        agent_id="misaka.fake-agent",
        name="Misaka Fake Agent",
        description="Standalone local A2A node backed by the deterministic Fake Agent",
        version="0.1.0",
        skills=(
            A2ASkill(
                skill_id="agent.invoke",
                name="Invoke local agent",
                description="Execute one explicit local agent invocation",
                capability_id=AGENT_CAPABILITY_ID,
                operation=AGENT_OPERATION_INVOKE,
                features=features,
                required_task_fields=frozenset({"provider_id", "model", "effort"}),
            ),
        ),
        features=features,
    )
    server = A2AServer(
        DelegationTaskHandler(
            delegation_runtime,
            card,
            provider_id=settings.provider_id,
        ),
        submission_timeout_seconds=15.0,
        shutdown_timeout_seconds=10.0,
    )
    return A2ANode(host, runtime, delegation_runtime, server, card, settings)
