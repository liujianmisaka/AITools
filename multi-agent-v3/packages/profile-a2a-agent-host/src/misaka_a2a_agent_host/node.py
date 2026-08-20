from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import TracebackType
from urllib.parse import urlsplit

from misaka_a2a_capability import A2AAgentCard, A2ASkill
from misaka_a2a_http import A2AHttpConfig, create_a2a_http_app
from misaka_a2a_runtime import A2AServer, A2AServerStatus, DelegationTaskHandler
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_agent_host_profile import AgentHost, AgentHostConfig, create_fake_agent_host
from misaka_delegation_runtime import DelegationRuntime
from misaka_fake_agent import FakeAgentScenario
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_invocation_contracts import CapabilityFeature
from misaka_kernel import CompositionSnapshot, HostStatus
from starlette.applications import Starlette


@dataclass(frozen=True, slots=True)
class A2AAgentHostConfig:
    host: str = "127.0.0.1"
    port: int = 8016
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
                "public_url is required when binding the A2A agent host to a wildcard address"
            )
        if self.public_url is not None:
            parsed = urlsplit(self.public_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("public_url must be an absolute HTTP(S) URL")

    @property
    def advertised_url(self) -> str:
        return self.public_url or f"http://{self.host}:{self.port}"


class A2AAgentHost:
    """Composes an Agent Host with A2A transport without changing either seam."""

    def __init__(
        self,
        agent_host: AgentHost,
        card: A2AAgentCard,
        config: A2AAgentHostConfig,
    ) -> None:
        self.agent_host = agent_host
        self.card = card
        self.config = config
        self.runtime = agent_host.runtime
        self.delegation_runtime = DelegationRuntime(
            self.runtime,
            MemoryInteractionChannelStore(),
        )
        self.server = A2AServer(
            DelegationTaskHandler(
                self.delegation_runtime,
                card,
                provider_id=agent_host.provider_id,
            ),
            submission_timeout_seconds=15.0,
            shutdown_timeout_seconds=10.0,
        )
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self.app: Starlette = create_a2a_http_app(
            self.server,
            card,
            config=A2AHttpConfig(public_url=config.advertised_url),
            start=self.start,
            stop=self.stop,
        )

    @property
    def host_status(self) -> HostStatus:
        return self.agent_host.status

    @property
    def server_status(self) -> A2AServerStatus:
        return self.server.status

    @property
    def composition_snapshot(self) -> CompositionSnapshot | None:
        return self.agent_host.composition_snapshot

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("A2A agent host cannot restart after stop")
            try:
                await self.agent_host.start()
                await self.server.start()
            except Exception:
                await self.server.stop()
                await self.agent_host.stop()
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
                    await self.agent_host.stop()

    async def __aenter__(self) -> A2AAgentHost:
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


def create_fake_a2a_agent_host(
    scenario: FakeAgentScenario | None = None,
    *,
    config: A2AAgentHostConfig | None = None,
) -> A2AAgentHost:
    settings = config or A2AAgentHostConfig()
    agent_host = create_fake_agent_host(
        scenario,
        provider_id=settings.provider_id,
        config=AgentHostConfig(
            profile_id="a2a-agent-host",
            profile_version=settings.profile_version,
            transport_ids=settings.transport_ids,
            fact_owners=(
                ("a2a.task", "runtime.a2a"),
                ("artifact.content", "capability.artifact.memory"),
                ("delegation.lifecycle", "runtime.delegation"),
                ("interaction.message", "capability.interaction.memory"),
                ("invocation.execution", "runtime.invocation"),
                ("session.log", "capability.session.memory"),
            ),
            projection_sources=(
                ("a2a.task.projection", "a2a.task"),
                ("delegation.snapshot", "delegation.lifecycle"),
                ("interaction.channel", "interaction.message"),
                ("invocation.snapshot", "invocation.execution"),
                ("session.snapshot", "session.log"),
            ),
            resource_owners=(
                ("a2a.task", "runtime.a2a"),
                ("artifact", "runtime.invocation"),
                ("delegation.channel", "runtime.delegation"),
                ("process", "runtime.invocation"),
                ("workspace", "runtime.invocation"),
            ),
        ),
    )
    features = frozenset(
        {
            CapabilityFeature.STRUCTURED_OUTPUT,
            CapabilityFeature.STREAMING,
            CapabilityFeature.CANCELLATION,
        }
    )
    card = A2AAgentCard(
        agent_id=f"misaka.agent-host.{settings.provider_id}",
        name="Misaka Agent Host",
        description="A2A endpoint backed by the standalone local Agent Host profile",
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
    return A2AAgentHost(agent_host, card, settings)
