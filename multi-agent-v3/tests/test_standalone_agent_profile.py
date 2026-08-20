from __future__ import annotations

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_fake_agent import FakeAgentScenario
from misaka_invocation_contracts import CompletionBoundary, InvocationRequest, InvocationStatus
from misaka_kernel import HostStatus
from misaka_standalone_agent import create_fake_standalone_agent


def _request(invocation_id: str) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "Run one standalone invocation"},
        idempotency_key=f"key-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
    )


@pytest.mark.asyncio
async def test_standalone_agent_runs_without_transport_or_control_plane() -> None:
    profile = create_fake_standalone_agent(FakeAgentScenario(output={"answer": "standalone-ok"}))

    async with profile:
        snapshot = profile.composition_snapshot
        assert snapshot is not None
        assert snapshot.profile_id == "standalone-agent"
        assert snapshot.transport_ids == ("in-process",)
        assert ("invocation.execution", "runtime.invocation") in snapshot.fact_owners
        assert ("invocation.snapshot", "invocation.execution") in snapshot.projection_sources
        assert ("workspace", "runtime.invocation") in snapshot.resource_owners

        result = await profile.run(_request("standalone-1"))

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "standalone-ok"}
    assert profile.status is HostStatus.STOPPED


def test_standalone_agent_config_rejects_duplicate_transports() -> None:
    from misaka_standalone_agent import StandaloneAgentConfig

    with pytest.raises(ValueError, match="unique"):
        StandaloneAgentConfig(transport_ids=("cli", "cli"))
