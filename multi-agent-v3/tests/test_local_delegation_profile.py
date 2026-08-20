from __future__ import annotations

import asyncio

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_delegation_contracts import (
    DelegationMode,
    DelegationRequest,
    DelegationStatus,
)
from misaka_fake_agent import FakeAgentScenario
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
from misaka_kernel import HostStatus
from misaka_local_delegation import create_fake_local_delegation


def _request(delegation_id: str) -> DelegationRequest:
    principal = PrincipalRef("local-user", PrincipalKind.HUMAN)
    return DelegationRequest(
        delegation_id=delegation_id,
        idempotency_key=f"key-{delegation_id}",
        initiator=principal,
        controller=principal,
        scope=ScopeRef("local-workspace"),
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "Run through local delegation"},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        mode=DelegationMode.CONTINUABLE,
    )


@pytest.mark.asyncio
async def test_local_delegation_profile_owns_delegation_and_message_facts() -> None:
    profile = create_fake_local_delegation(FakeAgentScenario(output={"answer": "delegated-ok"}))

    async with profile:
        snapshot = profile.composition_snapshot
        assert snapshot is not None
        assert snapshot.profile_id == "local-delegation"
        assert snapshot.transport_ids == ("in-process",)
        assert ("delegation.lifecycle", "runtime.delegation") in snapshot.fact_owners
        assert ("interaction.message", "capability.interaction.memory") in snapshot.fact_owners
        assert ("delegation.snapshot", "delegation.lifecycle") in snapshot.projection_sources
        assert ("delegation.channel", "runtime.delegation") in snapshot.resource_owners

        handle = await profile.submit(_request("delegation-local"))
        report = await handle.wait()
        durable_snapshot = await profile.snapshot(handle.delegation_id)

    assert report.status is DelegationStatus.COMPLETED
    assert report.output == {"answer": "delegated-ok"}
    assert durable_snapshot.ref.channel_id is not None
    assert durable_snapshot.ref.session_id is not None
    assert profile.status is HostStatus.STOPPED
    channel = await profile.channel_store.snapshot(durable_snapshot.ref.channel_id)
    assert channel.closed is True


@pytest.mark.asyncio
async def test_local_delegation_stop_cancels_active_activation() -> None:
    profile = create_fake_local_delegation(
        FakeAgentScenario(output={"answer": "late"}, delay_seconds=0.1)
    )
    await profile.start()
    handle = await profile.submit(_request("delegation-stop"))
    await asyncio.sleep(0)

    await profile.stop()
    report = await handle.wait()

    assert report.status is DelegationStatus.CANCELLED
    assert profile.status is HostStatus.STOPPED


@pytest.mark.asyncio
async def test_local_delegation_stop_before_start_is_safe_and_restart_is_explicitly_rejected() -> (
    None
):
    profile = create_fake_local_delegation()

    await profile.stop()
    await profile.start()
    await profile.stop()

    with pytest.raises(RuntimeError, match="cannot restart"):
        await profile.start()
