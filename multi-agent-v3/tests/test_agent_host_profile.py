from __future__ import annotations

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_agent_host_profile import create_fake_agent_host
from misaka_fake_agent import FakeAgentScenario
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationRequest,
    InvocationStatus,
)
from misaka_kernel import HostStatus
from misaka_policy_capability import StaticPolicyProvider
from misaka_policy_contracts import PolicyDecision, PolicyEffect


def _request(invocation_id: str) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "Run through the standalone Agent Host"},
        idempotency_key=f"key-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
    )


@pytest.mark.asyncio
async def test_agent_host_profile_executes_without_workflow_or_control_plane() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "profile-ok"}))

    async with host:
        assert host.status is HostStatus.ACTIVE
        snapshot = host.composition_snapshot
        assert snapshot is not None
        assert snapshot.profile_id == "agent-host"
        assert snapshot.transport_ids == ("in-process",)
        assert ("invocation.execution", "runtime.invocation") in snapshot.fact_owners
        assert ("invocation.snapshot", "invocation.execution") in snapshot.projection_sources
        assert ("process", "runtime.invocation") in snapshot.resource_owners
        result = await (await host.submit(_request("inv-profile"))).wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "profile-ok"}
    assert host.status is HostStatus.STOPPED


@pytest.mark.asyncio
async def test_agent_host_stop_cancels_active_invocation_and_waits_for_terminal() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "late"}, delay_seconds=0.05))
    await host.start()
    handle = await host.submit(_request("inv-stop"))

    await host.stop()
    result = await handle.wait()

    assert result.status is InvocationStatus.CANCELLED
    assert host.status is HostStatus.STOPPED


@pytest.mark.asyncio
async def test_agent_host_profiles_are_isolated() -> None:
    first = create_fake_agent_host(FakeAgentScenario(output={"host": "first"}))
    second = create_fake_agent_host(FakeAgentScenario(output={"host": "second"}))
    await first.start()
    await second.start()

    first_result = await (await first.submit(_request("first"))).wait()
    second_result = await (await second.submit(_request("second"))).wait()

    assert first_result.output == {"host": "first"}
    assert second_result.output == {"host": "second"}
    await first.stop()
    await second.stop()


@pytest.mark.asyncio
async def test_agent_host_policy_rejects_before_provider_execution() -> None:
    policy = StaticPolicyProvider(
        PolicyDecision(PolicyEffect.DENY, "workspace access denied by test policy")
    )
    host = create_fake_agent_host(policy_provider=policy)
    await host.start()

    result = await (await host.submit(_request("denied"))).wait()

    assert result.status is InvocationStatus.REJECTED
    assert result.error_code == "policy.denied"
    assert policy.evaluations == 1
    await host.stop()
