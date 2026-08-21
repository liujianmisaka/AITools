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
from misaka_kernel import (
    CompositionSnapshot,
    Host,
    HostStatus,
    ProfileDefinition,
    ProfileLoader,
)
from starlette.applications import Starlette


@dataclass(frozen=True, slots=True)
class A2ANodeConfig:
    host: str = "127.0.0.1"
    port: int = 8015
    public_url: str | None = None
    provider_id: str = "fake-agent"
    profile_version: str = "1.0.0"
    transport_ids: tuple[str, ...] = ("a2a-http", "a2a-sse")

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if not self.profile_version.strip():
            raise ValueError("profile_version must not be empty")
        if any(not item.strip() for item in self.transport_ids):
            raise ValueError("transport ids must not be empty")
        if len(self.transport_ids) != len(set(self.transport_ids)):
            raise ValueError("transport ids must be unique")
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
        self._started = False
        self._closed = False
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

    @property
    def composition_snapshot(self) -> CompositionSnapshot | None:
        return self._host.composition_snapshot

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("A2A node cannot restart after stop")
            try:
                await self._host.start()
                await self.server.start()
            except Exception:
                await self.server.stop()
                await self._host.stop()
                self._closed = True
                raise
            self._started = True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._started:
                return
            self._started = False
            self._closed = True
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
        profile_version=settings.profile_version,
        module_ids=(INVOCATION_RUNTIME_MODULE_ID, FAKE_AGENT_MODULE_ID),
        bindings={AGENT_PROVIDER_SERVICE: settings.provider_id},
        transport_ids=settings.transport_ids,
        fact_owners={
            "a2a.task": "runtime.a2a",
            "delegation.lifecycle": "runtime.delegation",
            "interaction.message": "capability.interaction.memory",
            "invocation.execution": "runtime.invocation",
        },
        projection_sources={
            "a2a.task.projection": "a2a.task",
            "delegation.snapshot": "delegation.lifecycle",
            "interaction.channel": "interaction.message",
            "invocation.snapshot": "invocation.execution",
        },
        resource_owners={
            "a2a.task": "runtime.a2a",
            "delegation.channel": "runtime.delegation",
            "process": "runtime.invocation",
        },
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
            CapabilityFeature.RESUME,
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
