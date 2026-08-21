from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType

from misaka_agent_host_profile import AgentHost, AgentHostConfig, create_fake_agent_host
from misaka_approval_capability import DecisionGate
from misaka_delegation_capability import (
    DelegationGate,
    DelegationHandle,
    DelegationStore,
)
from misaka_delegation_contracts import (
    ContinuationRequest,
    DelegationRequest,
    DelegationSnapshot,
)
from misaka_delegation_runtime import DelegationRuntime
from misaka_fake_agent import FakeAgentScenario
from misaka_interaction_capability import InteractionChannelStore
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_kernel import CompositionSnapshot, HostStatus
from misaka_persistence_contracts import SessionLog
from misaka_policy_contracts import PolicyProvider

_FACT_OWNERS = (
    ("artifact.content", "capability.artifact.memory"),
    ("delegation.lifecycle", "runtime.delegation"),
    ("interaction.message", "capability.interaction.memory"),
    ("invocation.execution", "runtime.invocation"),
)
_PROJECTION_SOURCES = (
    ("delegation.snapshot", "delegation.lifecycle"),
    ("interaction.channel", "interaction.message"),
    ("invocation.snapshot", "invocation.execution"),
)
_RESOURCE_OWNERS = (
    ("artifact", "runtime.invocation"),
    ("delegation.channel", "runtime.delegation"),
    ("process", "runtime.invocation"),
    ("workspace", "runtime.invocation"),
)


@dataclass(frozen=True, slots=True)
class LocalDelegationConfig:
    profile_id: str = "local-delegation"
    profile_version: str = "1.0.0"
    transport_ids: tuple[str, ...] = ("in-process",)

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.profile_version.strip():
            raise ValueError("local delegation profile identity must not be empty")
        if any(not item.strip() for item in self.transport_ids):
            raise ValueError("local delegation transport ids must not be empty")
        if len(self.transport_ids) != len(set(self.transport_ids)):
            raise ValueError("local delegation transport ids must be unique")


class LocalDelegationProfile(AbstractAsyncContextManager["LocalDelegationProfile"]):
    """In-process Delegation gateway over an explicitly owned Agent Host."""

    def __init__(
        self,
        agent_host: AgentHost,
        runtime: DelegationRuntime,
        channel_store: InteractionChannelStore,
    ) -> None:
        self.agent_host = agent_host
        self.runtime = runtime
        self.channel_store = channel_store
        self._started = False
        self._closed = False
        self._channel_ids: set[str] = set()
        self._lifecycle_lock = asyncio.Lock()

    @property
    def status(self) -> HostStatus:
        return self.agent_host.status

    @property
    def composition_snapshot(self) -> CompositionSnapshot | None:
        return self.agent_host.composition_snapshot

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("local delegation profile cannot restart after stop")
            await self.agent_host.start()
            self._started = True

    async def submit(self, request: DelegationRequest) -> DelegationHandle:
        self._require_started()
        handle = await self.runtime.submit(request)
        snapshot = await handle.snapshot()
        if snapshot.ref.channel_id is not None:
            self._channel_ids.add(snapshot.ref.channel_id)
        return handle

    async def continue_request(self, request: ContinuationRequest) -> DelegationHandle:
        self._require_started()
        return await self.runtime.continue_request(request)

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot:
        self._require_started()
        return await self.runtime.snapshot(delegation_id)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._started:
                return
            self._started = False
            self._closed = True
            try:
                await self.runtime.stop()
            finally:
                try:
                    if self._channel_ids:
                        await asyncio.gather(
                            *(
                                self.channel_store.close(channel_id)
                                for channel_id in self._channel_ids
                            ),
                            return_exceptions=True,
                        )
                        self._channel_ids.clear()
                finally:
                    await self.agent_host.stop()

    async def __aenter__(self) -> LocalDelegationProfile:
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

    def _require_started(self) -> None:
        if not self._started or self.agent_host.status is not HostStatus.ACTIVE:
            raise RuntimeError("local delegation profile must be active before use")


def create_fake_local_delegation(
    scenario: FakeAgentScenario | None = None,
    *,
    provider_id: str = "fake-agent",
    policy_provider: PolicyProvider | None = None,
    decision_gate: DecisionGate | None = None,
    delegation_gate: DelegationGate | None = None,
    delegation_store: DelegationStore | None = None,
    channel_store: InteractionChannelStore | None = None,
    session_log: SessionLog | None = None,
    config: LocalDelegationConfig | None = None,
) -> LocalDelegationProfile:
    settings = config or LocalDelegationConfig()
    fact_owners = _FACT_OWNERS
    projection_sources = _PROJECTION_SOURCES
    if session_log is not None:
        fact_owners += (("session.fact", "persistence.session"),)
        projection_sources += (("session.projection", "session.fact"),)
    agent_host = create_fake_agent_host(
        scenario,
        provider_id=provider_id,
        policy_provider=policy_provider,
        decision_gate=decision_gate,
        config=AgentHostConfig(
            profile_id=settings.profile_id,
            profile_version=settings.profile_version,
            transport_ids=settings.transport_ids,
            fact_owners=fact_owners,
            projection_sources=projection_sources,
            resource_owners=_RESOURCE_OWNERS,
        ),
    )
    selected_channel_store = channel_store or MemoryInteractionChannelStore()
    runtime = DelegationRuntime(
        agent_host.runtime,
        selected_channel_store,
        store=delegation_store,
        gate=delegation_gate,
        session_log=session_log,
        composition_id=settings.profile_id,
    )
    return LocalDelegationProfile(agent_host, runtime, selected_channel_store)
