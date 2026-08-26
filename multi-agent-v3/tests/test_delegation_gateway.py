from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_agent_host_profile import create_fake_agent_host
from misaka_delegation_capability import DelegationStateError, DelegationUnauthorized
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationMode,
    DelegationReconciliationResolution,
    DelegationRef,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
    MessageDispatchMode,
    MessageDispatchRequest,
    MessageDispatchStatus,
)
from misaka_delegation_runtime import (
    DelegationRuntime,
    MemoryDelegationStore,
    RuntimeDelegationGateway,
)
from misaka_fake_agent import FakeAgentScenario, FakeFailure
from misaka_interaction_contracts import (
    InteractionMessageDraft,
    MessageCursor,
    MessageType,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_interaction_memory import MemoryInteractionChannelStore


def _principal(principal_id: str, kind: PrincipalKind = PrincipalKind.APPLICATION) -> PrincipalRef:
    return PrincipalRef(principal_id, kind)


def _request(delegation_id: str, *, observer: PrincipalRef | None = None) -> DelegationRequest:
    controller = _principal("controller")
    return DelegationRequest(
        delegation_id=delegation_id,
        idempotency_key=f"idem-{delegation_id}",
        initiator=controller,
        controller=controller,
        scope=ScopeRef("gateway-scope"),
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "return the fake answer"},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        mode=DelegationMode.CONTINUABLE,
        observers=(observer,) if observer is not None else (),
    )


@pytest.mark.asyncio
async def test_gateway_authorizes_observers_and_supports_reply_with_cursor_replay() -> None:
    observer = _principal("observer", PrincipalKind.HUMAN)
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(host.runtime, channels)
    gateway = RuntimeDelegationGateway(runtime, channels)
    await host.start()
    try:
        with pytest.raises(DelegationUnauthorized, match="declared initiator"):
            await gateway.create(
                _request("gateway-spoofed-initiator"),
                _principal("intruder"),
            )
        created = await gateway.create(
            _request("gateway-reply", observer=observer),
            _principal("controller"),
        )
        terminal = await _wait_terminal(gateway, created.ref.delegation_id, observer)
        assert terminal.status is DelegationStatus.COMPLETED

        events = await gateway.events(created.ref.delegation_id, observer)
        assert events
        assert events[-1].message_type is MessageType.RESULT

        with pytest.raises(DelegationUnauthorized):
            await gateway.get(created.ref.delegation_id, _principal("intruder"))
        with pytest.raises(DelegationUnauthorized):
            await gateway.get(
                created.ref.delegation_id,
                _principal(observer.principal_id, PrincipalKind.APPLICATION),
            )

        channel_id = terminal.ref.channel_id
        child_scope = terminal.ref.child_scope
        assert channel_id is not None
        assert child_scope is not None
        child = _principal(
            f"delegation:{terminal.ref.delegation_id}",
            PrincipalKind.AGENT,
        )
        question = await gateway.send(
            terminal.ref.delegation_id,
            child,
            InteractionMessageDraft(
                message_id="gateway-question",
                channel_id=channel_id,
                sender=child,
                recipient=terminal.request.controller,
                message_type=MessageType.QUESTION,
                payload={"question": "continue?"},
                scope=child_scope,
                correlation_id="gateway-correlation",
            ),
        )
        waiting = await gateway.get(terminal.ref.delegation_id, terminal.request.controller)
        assert waiting.status is DelegationStatus.WAITING_INPUT

        await gateway.reply(
            ContinuationRequest(
                request_id="gateway-reply-request",
                delegation_id=terminal.ref.delegation_id,
                operation=ContinuationOperation.REPLY,
                actor=terminal.request.controller,
                idempotency_key="gateway-reply-idem",
                session_id=terminal.ref.session_id,
                message_id="gateway-answer",
                expected_activation_id=terminal.report_history[-1].source_activation_id,
                input={"answer": "continue"},
                correlation_id="gateway-correlation",
                reply_to=question.message_id,
            )
        )
        second = await _wait_terminal(
            gateway,
            terminal.ref.delegation_id,
            terminal.request.controller,
            activation_count=2,
        )
        assert second.status is DelegationStatus.COMPLETED
        assert second.activation_count == 2

        replay = await gateway.events(
            terminal.ref.delegation_id,
            observer,
            cursor=MessageCursor(channel_id, question.sequence),
        )
        assert replay[0].message_id == question.message_id

        with pytest.raises(DelegationUnauthorized):
            await gateway.send(
                terminal.ref.delegation_id,
                observer,
                InteractionMessageDraft(
                    message_id="observer-send",
                    channel_id=channel_id,
                    sender=observer,
                    message_type=MessageType.INSTRUCTION,
                    payload={"prompt": "forbidden"},
                    scope=child_scope,
                ),
            )
    finally:
        await runtime.stop()
        await host.stop()


@pytest.mark.asyncio
async def test_gateway_reconcile_and_cancel_require_matching_operations() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "late"}, delay_seconds=0.2))
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(host.runtime, channels)
    gateway = RuntimeDelegationGateway(runtime, channels)
    controller = _principal("controller")
    await host.start()
    try:
        created = await gateway.create(
            _request("gateway-cancel"),
            controller,
        )
        active = await _wait_active(gateway, created.ref.delegation_id, controller)
        assert active.current_activation_id is not None

        reconciled = await gateway.reconcile(
            ContinuationRequest(
                request_id="gateway-reconcile",
                delegation_id=created.ref.delegation_id,
                operation=ContinuationOperation.RECONCILE,
                actor=controller,
                idempotency_key="gateway-reconcile-idem",
                session_id=active.ref.session_id,
                expected_activation_id=active.current_activation_id,
            )
        )
        assert reconciled.status is DelegationStatus.ACTIVE

        cancelled = await gateway.cancel(
            ContinuationRequest(
                request_id="gateway-cancel",
                delegation_id=created.ref.delegation_id,
                operation=ContinuationOperation.CANCEL,
                actor=controller,
                idempotency_key="gateway-cancel-idem",
                session_id=active.ref.session_id,
                expected_activation_id=active.current_activation_id,
                input={"reason": "cancelled by gateway test"},
            )
        )
        terminal = await _wait_terminal(gateway, cancelled.ref.delegation_id, controller)
        assert terminal.status is DelegationStatus.CANCELLED

        with pytest.raises(ValueError, match="matching continuation operation"):
            await gateway.cancel(
                ContinuationRequest(
                    request_id="wrong-operation",
                    delegation_id=created.ref.delegation_id,
                    operation=ContinuationOperation.RECONCILE,
                    actor=controller,
                    idempotency_key="wrong-operation-idem",
                    session_id=active.ref.session_id,
                )
            )
    finally:
        await runtime.stop()
        await host.stop()


@pytest.mark.asyncio
async def test_gateway_dispatches_controller_messages_and_rejects_observers() -> None:
    observer = _principal("observer", PrincipalKind.HUMAN)
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "late"}, delay_seconds=0.2))
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(host.runtime, channels)
    gateway = RuntimeDelegationGateway(runtime, channels)
    controller = _principal("controller")
    await host.start()
    try:
        created = await gateway.create(
            _request("gateway-dispatch", observer=observer),
            controller,
        )
        active = await _wait_active(gateway, created.ref.delegation_id, controller)
        assert active.ref.session_id is not None
        assert active.current_activation_id is not None
        request = MessageDispatchRequest(
            dispatch_id="gateway-dispatch-1",
            delegation_id=created.ref.delegation_id,
            idempotency_key="gateway-dispatch-idem",
            message_id="gateway-dispatch-message",
            actor=controller,
            session_id=active.ref.session_id,
            expected_activation_id=active.current_activation_id,
            delivery=MessageDispatchMode.APPEND,
            message_type=MessageType.INSTRUCTION,
            payload={"prompt": "continue with this instruction"},
        )

        with pytest.raises(DelegationUnauthorized):
            await gateway.dispatch_message(replace(request, actor=observer))

        dispatch = await gateway.dispatch_message(request)
        assert dispatch.status is MessageDispatchStatus.QUEUED
        assert dispatch.previous_activation_id == active.current_activation_id
    finally:
        await runtime.stop()
        await host.stop()


@pytest.mark.asyncio
async def test_gateway_manually_resolves_only_uncertain_terminal_report() -> None:
    host = create_fake_agent_host(
        FakeAgentScenario(
            failure=FakeFailure(
                "fake.external_unknown",
                "external completion could not be proven",
                reconciliation_required=True,
            )
        )
    )
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(host.runtime, channels)
    gateway = RuntimeDelegationGateway(runtime, channels)
    controller = _principal("controller")
    await host.start()
    try:
        created = await gateway.create(_request("gateway-manual-resolution"), controller)
        uncertain = await _wait_terminal(gateway, created.ref.delegation_id, controller)
        assert uncertain.status is DelegationStatus.RECONCILIATION_REQUIRED
        assert uncertain.report is not None

        resolution = DelegationReconciliationResolution(
            request_id="manual-resolution-1",
            delegation_id=created.ref.delegation_id,
            actor=controller,
            idempotency_key="manual-resolution-idem",
            expected_revision=uncertain.revision,
            status=DelegationStatus.COMPLETED,
            reason="confirmed from the external Agent session",
            output={"answer": "verified"},
        )
        with pytest.raises(DelegationStateError, match="revision changed"):
            await gateway.resolve_reconciliation(
                replace(
                    resolution,
                    request_id="manual-resolution-stale",
                    idempotency_key="manual-resolution-stale",
                    expected_revision=uncertain.revision - 1,
                )
            )
        resolved = await gateway.resolve_reconciliation(resolution)
        repeated = await gateway.resolve_reconciliation(resolution)

        assert resolved.status is DelegationStatus.COMPLETED
        assert resolved.report is not None
        assert resolved.report.output == {"answer": "verified"}
        assert resolved.report.source_invocation_id == uncertain.report.source_invocation_id
        assert resolved.report.resolution_reason == resolution.reason
        assert resolved.report.resolved_by == controller
        assert resolved.report_history[-2:] == (uncertain.report, resolved.report)
        assert repeated == resolved

        with pytest.raises(DelegationStateError, match="only a reconciliation_required"):
            await gateway.resolve_reconciliation(
                replace(
                    resolution,
                    request_id="manual-resolution-rewrite",
                    idempotency_key="manual-resolution-rewrite",
                    expected_revision=resolved.revision,
                )
            )

        with pytest.raises(DelegationUnauthorized):
            await gateway.resolve_reconciliation(
                replace(
                    resolution,
                    request_id="manual-resolution-intruder",
                    idempotency_key="manual-resolution-intruder",
                    actor=_principal("intruder"),
                    expected_revision=resolved.revision,
                )
            )
    finally:
        await runtime.stop()
        await host.stop()


@pytest.mark.asyncio
async def test_gateway_lists_children_in_attachment_order_for_parent_observer() -> None:
    observer = _principal("observer", PrincipalKind.HUMAN)
    store = MemoryDelegationStore()
    channels = MemoryInteractionChannelStore()
    host = create_fake_agent_host()
    runtime = DelegationRuntime(host.runtime, channels, store=store)
    gateway = RuntimeDelegationGateway(runtime, channels)
    parent_request = _request("parent", observer=observer)
    parent_ref = DelegationRef(parent_request.delegation_id)
    await store.create(parent_request, parent_ref)
    child_ids = ["child-b", "child-a"]
    for child_id in child_ids:
        child_request = replace(
            _request(child_id),
            parent_delegation_id=parent_request.delegation_id,
        )
        child_ref = DelegationRef(
            child_id,
            parent_delegation_id=parent_request.delegation_id,
            depth=1,
        )
        await store.create(child_request, child_ref)
        await store.attach_child(parent_request.delegation_id, child_ref)

    children = await gateway.children(parent_request.delegation_id, observer)
    assert [child.ref.delegation_id for child in children] == child_ids

    with pytest.raises(DelegationUnauthorized):
        await gateway.children(
            parent_request.delegation_id,
            _principal(observer.principal_id, PrincipalKind.APPLICATION),
        )
    with pytest.raises(DelegationUnauthorized):
        await gateway.children(parent_request.delegation_id, _principal("intruder"))


async def _wait_terminal(
    gateway: RuntimeDelegationGateway,
    delegation_id: str,
    actor: PrincipalRef,
    *,
    activation_count: int = 1,
) -> DelegationSnapshot:
    for _ in range(100):
        snapshot = await gateway.get(delegation_id, actor)
        if snapshot.report is not None and snapshot.activation_count >= activation_count:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError("delegation did not become terminal")


async def _wait_active(
    gateway: RuntimeDelegationGateway,
    delegation_id: str,
    actor: PrincipalRef,
) -> DelegationSnapshot:
    for _ in range(100):
        snapshot = await gateway.get(delegation_id, actor)
        if snapshot.status is DelegationStatus.ACTIVE:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError("delegation did not become active")
