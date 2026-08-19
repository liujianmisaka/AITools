from __future__ import annotations

import asyncio

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_agent_host_profile import create_fake_agent_host
from misaka_delegation_capability import (
    DelegationCapabilityRejected,
    DelegationStateError,
    DelegationUnauthorized,
)
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationMode,
    DelegationRequest,
    DelegationStatus,
)
from misaka_delegation_runtime import DelegationRuntime
from misaka_fake_agent import FakeAgentScenario
from misaka_interaction_contracts import (
    MessageCursor,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_interaction_memory import MemoryInteractionChannelStore


def _principal(principal_id: str = "parent") -> PrincipalRef:
    return PrincipalRef(principal_id, PrincipalKind.APPLICATION)


def _request(
    delegation_id: str,
    *,
    mode: DelegationMode = DelegationMode.ONE_SHOT,
    provider_id: str = "fake-agent",
) -> DelegationRequest:
    return DelegationRequest(
        delegation_id=delegation_id,
        idempotency_key=f"idem-{delegation_id}",
        initiator=_principal(),
        controller=_principal(),
        scope=ScopeRef("scope-1"),
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "return the fake answer"},
        provider_id=provider_id,
        model="fake/model",
        effort="high",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        mode=mode,
    )


@pytest.mark.asyncio
async def test_one_shot_delegation_maps_invocation_result_and_is_idempotent() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(host.runtime, channels)
    await host.start()

    first = await runtime.submit(_request("delegation-one"))
    duplicate = await runtime.submit(_request("delegation-one"))
    report = await first.wait()
    duplicate_report = await duplicate.wait()
    snapshot = await first.snapshot()

    assert report.status is DelegationStatus.COMPLETED
    assert report.output == {"answer": "ok"}
    assert duplicate_report == report
    assert snapshot.activation_count == 1
    assert snapshot.current_invocation_id is None
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_unsupported_feature_is_a_terminal_rejection_not_a_stuck_delegation() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    request = _request("delegation-rejected")
    request = DelegationRequest(
        delegation_id=request.delegation_id,
        idempotency_key=request.idempotency_key,
        initiator=request.initiator,
        controller=request.controller,
        scope=request.scope,
        capability_id=request.capability_id,
        operation=request.operation,
        input=request.input,
        provider_id=request.provider_id,
        model=request.model,
        effort=request.effort,
        output_schema=request.output_schema,
        required_features=frozenset({"unknown-feature"}),
    )

    handle = await runtime.submit(request)
    report = await handle.wait()

    assert report.status is DelegationStatus.REJECTED
    assert report.error_code == "delegation.feature_unsupported"
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_continuable_delegation_supports_cursor_messages_and_follow_up() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(host.runtime, channels)
    await host.start()

    handle = await runtime.submit(_request("delegation-cont", mode=DelegationMode.CONTINUABLE))
    first_report = await handle.wait()
    first_snapshot = await handle.snapshot()
    messages = await channels.read(first_snapshot.ref.channel_id or "")

    assert first_report.status is DelegationStatus.COMPLETED
    assert first_snapshot.ref.session_id is not None
    assert first_snapshot.ref.channel_id is not None
    assert messages
    assert messages[-1].sequence == len(messages)

    follow_up = await handle.continue_request(
        ContinuationRequest(
            request_id="continuation-1",
            delegation_id="delegation-cont",
            operation=ContinuationOperation.FOLLOW_UP,
            actor=_principal(),
            idempotency_key="continuation-idem-1",
            session_id=first_snapshot.ref.session_id,
            message_id="follow-up-message-1",
            input={"prompt": "continue"},
        )
    )
    second_report = await follow_up.wait()
    second_snapshot = await follow_up.snapshot()

    assert second_report.status is DelegationStatus.COMPLETED
    assert second_snapshot.activation_count == 2
    assert len(second_snapshot.report_history) == 2
    replay = await channels.read(
        second_snapshot.ref.channel_id or "",
        cursor=MessageCursor(second_snapshot.ref.channel_id or "", 1),
    )
    assert any(message.message_id == "follow-up-message-1" for message in replay)
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_delegation_rejects_unauthorized_and_unsupported_continuation() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    handle = await runtime.submit(_request("delegation-auth", mode=DelegationMode.CONTINUABLE))
    await handle.wait()
    snapshot = await handle.snapshot()

    with pytest.raises(DelegationUnauthorized):
        await handle.continue_request(
            ContinuationRequest(
                request_id="bad-actor",
                delegation_id="delegation-auth",
                operation=ContinuationOperation.RECONCILE,
                actor=_principal("intruder"),
                idempotency_key="bad-actor-key",
            )
        )

    with pytest.raises(DelegationCapabilityRejected):
        await handle.continue_request(
            ContinuationRequest(
                request_id="steer-1",
                delegation_id="delegation-auth",
                operation=ContinuationOperation.STEER,
                actor=_principal(),
                idempotency_key="steer-key",
                session_id=snapshot.ref.session_id,
                message_id="steer-message",
                input={"text": "steer"},
            )
        )
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_cancel_waits_for_provider_terminal_state() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "late"}, delay_seconds=0.05))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    handle = await runtime.submit(_request("delegation-cancel"))
    await asyncio.sleep(0)

    await handle.cancel("parent", "user cancelled")
    report = await handle.wait()

    assert report.status is DelegationStatus.CANCELLED
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_two_follow_ups_cannot_create_parallel_live_activation() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "late"}, delay_seconds=0.05))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    handle = await runtime.submit(_request("delegation-serial", mode=DelegationMode.CONTINUABLE))
    await handle.wait()
    snapshot = await handle.snapshot()

    first = ContinuationRequest(
        request_id="serial-1",
        delegation_id="delegation-serial",
        operation=ContinuationOperation.FOLLOW_UP,
        actor=_principal(),
        idempotency_key="serial-key-1",
        session_id=snapshot.ref.session_id,
        message_id="serial-message-1",
        input={"prompt": "first"},
    )
    second = ContinuationRequest(
        request_id="serial-2",
        delegation_id="delegation-serial",
        operation=ContinuationOperation.FOLLOW_UP,
        actor=_principal(),
        idempotency_key="serial-key-2",
        session_id=snapshot.ref.session_id,
        message_id="serial-message-2",
        input={"prompt": "second"},
    )
    await runtime.continue_request(first)
    with pytest.raises(DelegationStateError):
        await runtime.continue_request(second)
    await handle.wait()
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_same_continuation_key_is_idempotent_even_with_a_new_request_id() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    handle = await runtime.submit(_request("delegation-cont-idem", mode=DelegationMode.CONTINUABLE))
    await handle.wait()
    snapshot = await handle.snapshot()
    first_request = ContinuationRequest(
        request_id="continuation-a",
        delegation_id="delegation-cont-idem",
        operation=ContinuationOperation.FOLLOW_UP,
        actor=_principal(),
        idempotency_key="same-continuation-key",
        session_id=snapshot.ref.session_id,
        message_id="same-message",
        input={"prompt": "continue"},
    )
    second_request = ContinuationRequest(
        request_id="continuation-b",
        delegation_id=first_request.delegation_id,
        operation=first_request.operation,
        actor=first_request.actor,
        idempotency_key=first_request.idempotency_key,
        session_id=first_request.session_id,
        message_id=first_request.message_id,
        input=first_request.input,
    )
    first_handle = await runtime.continue_request(first_request)
    second_handle = await runtime.continue_request(second_request)

    assert await first_handle.wait() == await second_handle.wait()
    assert (await first_handle.snapshot()).activation_count == 2
    await runtime.stop()
    await host.stop()
