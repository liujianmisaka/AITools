from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType

from misaka_agent_host_profile import AgentHost, AgentHostConfig, create_fake_agent_host
from misaka_approval_capability import DecisionGate
from misaka_fake_agent import FakeAgentScenario
from misaka_invocation_contracts import InvocationRequest, InvocationResult
from misaka_kernel import CompositionSnapshot, HostStatus
from misaka_policy_contracts import PolicyProvider


@dataclass(frozen=True, slots=True)
class StandaloneAgentConfig:
    profile_id: str = "standalone-agent"
    profile_version: str = "1.0.0"
    transport_ids: tuple[str, ...] = ("in-process",)

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.profile_version.strip():
            raise ValueError("standalone agent profile identity must not be empty")
        if any(not item.strip() for item in self.transport_ids):
            raise ValueError("standalone agent transport ids must not be empty")
        if len(self.transport_ids) != len(set(self.transport_ids)):
            raise ValueError("standalone agent transport ids must be unique")


class StandaloneAgent(AbstractAsyncContextManager["StandaloneAgent"]):
    """Minimal local profile for explicit Agent invocations."""

    def __init__(self, agent_host: AgentHost) -> None:
        self.agent_host = agent_host

    @property
    def status(self) -> HostStatus:
        return self.agent_host.status

    @property
    def composition_snapshot(self) -> CompositionSnapshot | None:
        return self.agent_host.composition_snapshot

    async def start(self) -> None:
        await self.agent_host.start()

    async def run(self, request: InvocationRequest) -> InvocationResult:
        return await (await self.agent_host.submit(request)).wait()

    async def stop(self) -> None:
        await self.agent_host.stop()

    async def __aenter__(self) -> StandaloneAgent:
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


def create_fake_standalone_agent(
    scenario: FakeAgentScenario | None = None,
    *,
    provider_id: str = "fake-agent",
    policy_provider: PolicyProvider | None = None,
    decision_gate: DecisionGate | None = None,
    config: StandaloneAgentConfig | None = None,
) -> StandaloneAgent:
    settings = config or StandaloneAgentConfig()
    agent_host = create_fake_agent_host(
        scenario,
        provider_id=provider_id,
        policy_provider=policy_provider,
        decision_gate=decision_gate,
        config=AgentHostConfig(
            profile_id=settings.profile_id,
            profile_version=settings.profile_version,
            transport_ids=settings.transport_ids,
        ),
    )
    return StandaloneAgent(agent_host)
