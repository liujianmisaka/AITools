from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_agent_host_profile import create_fake_agent_host
from misaka_delegation_capability import (
    DelegationCapabilityRejected,
    DelegationStateError,
    DelegationUnauthorized,
    StaticDelegationGate,
)
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationAdmission,
    DelegationBudget,
    DelegationMode,
    DelegationPolicy,
    DelegationRef,
    DelegationRequest,
    DelegationStatus,
)
from misaka_delegation_runtime import DelegationRuntime, MemoryDelegationStore
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario, FakeFailure
from misaka_interaction_contracts import (
    InteractionMessageDraft,
    MessageCursor,
    MessageDeliveryStatus,
    MessageType,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_invocation_contracts import (
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ReconcileResult,
    ReconcileStatus,
    SessionRef,
)
from misaka_invocation_runtime import InvocationRuntime
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableConflict, DurableNotFound, SessionHeader
from misaka_persistence_jsonl import JsonlEventLog, JsonlSessionLog


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


class _ControllableExecution:
    def __init__(
        self,
        invocation_id: str,
        events: tuple[InvocationEvent, ...] = (),
    ) -> None:
        self.invocation_id = invocation_id
        self.activation_id = f"{invocation_id}:activation"
        self.events_history = events
        self.done = asyncio.Event()
        self.cancelled = False
        self.steers: list[JsonObject] = []
        self.pauses: list[JsonObject] = []
        self.resumes: list[JsonObject] = []

    def events(self) -> AsyncIterator[InvocationEvent]:
        async def _events() -> AsyncIterator[InvocationEvent]:
            for event in self.events_history:
                yield event

        return _events()

    async def wait(self) -> InvocationResult:
        await self.done.wait()
        return InvocationResult(
            invocation_id=self.invocation_id,
            status=InvocationStatus.CANCELLED if self.cancelled else InvocationStatus.SUCCEEDED,
            output=None if self.cancelled else {"answer": "controlled"},
            error_code="cancelled" if self.cancelled else None,
        )

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("reason must not be empty")
        self.cancelled = True
        self.done.set()

    async def reconcile(self) -> ReconcileResult:
        return ReconcileResult(
            ReconcileStatus.CANCELLED if self.cancelled else ReconcileStatus.RUNNING
        )

    async def steer(self, input_value: JsonObject) -> None:
        self.steers.append(input_value)

    async def pause(self, input_value: JsonObject) -> None:
        self.pauses.append(input_value)

    async def resume(self, input_value: JsonObject) -> None:
        self.resumes.append(input_value)

    def finish(self) -> None:
        self.done.set()


class _ControllableExecutionPort:
    def __init__(
        self,
        provider_session_ids: tuple[str | None, ...] = (),
    ) -> None:
        self.provider_session_ids = provider_session_ids
        self.handles: list[_ControllableExecution] = []
        self.requests: list[InvocationRequest] = []

    async def submit(
        self, request: InvocationRequest, *, provider_id: str | None = None
    ) -> _ControllableExecution:
        index = len(self.handles)
        self.requests.append(request)
        invocation_id = request.invocation_id
        session_id = (
            self.provider_session_ids[index] if index < len(self.provider_session_ids) else None
        )
        events: tuple[InvocationEvent, ...] = ()
        if session_id is not None:
            events = (
                InvocationEvent(
                    invocation_id=invocation_id,
                    sequence=1,
                    status=InvocationStatus.RUNNING,
                    payload={
                        "provider_id": provider_id or "fake-agent",
                        "provider_session_id": session_id,
                        "provider_operation_id": f"operation-{index + 1}",
                    },
                ),
            )
        handle = _ControllableExecution(invocation_id, events)
        self.handles.append(handle)
        return handle


class _ReadFailingSessionLog(JsonlSessionLog):
    async def get(self, session_id: str) -> SessionHeader:
        del session_id
        raise RuntimeError("session log read failed")


class _CreateConflictingSessionLog(JsonlSessionLog):
    async def get(self, session_id: str) -> SessionHeader:
        raise DurableNotFound(
            "session.not_found",
            f"session {session_id} was not found",
        )

    async def create(self, header: SessionHeader) -> SessionHeader:
        del header
        raise DurableConflict(
            "session.unsupported_format",
            "session format cannot be created",
        )


@pytest.mark.asyncio
async def test_continuable_session_log_persists_header_binding_and_events(
    tmp_path: Path,
) -> None:
    port = _ControllableExecutionPort(("provider-session-1",))
    session_log = JsonlSessionLog(JsonlEventLog(tmp_path / "sessions.jsonl"))
    runtime = DelegationRuntime(
        port,
        MemoryInteractionChannelStore(),
        session_log=session_log,
        composition_id="test-composition",
    )
    request = _request("delegation-session-log", mode=DelegationMode.CONTINUABLE)

    handle = await runtime.submit(request)
    port.handles[0].finish()
    report = await handle.wait()
    snapshot = await handle.snapshot()
    session_id = snapshot.ref.session_id

    assert report.status is DelegationStatus.COMPLETED
    assert session_id is not None
    header = await session_log.get(session_id)
    assert header.session_id == session_id
    assert header.owner_id == request.controller.principal_id
    assert header.scope_id == (snapshot.ref.child_scope or request.scope).scope_id
    assert header.composition_id == "test-composition"
    assert header.metadata == {"delegation_id": request.delegation_id}

    facts = await session_log.read(session_id)
    binding = next(fact for fact in facts if fact.event_type == "delegation.provider_session_bound")
    assert binding.event_id == ("delegation:delegation-session-log:provider-binding")
    assert binding.payload == {
        "delegation_id": request.delegation_id,
        "provider_id": "fake-agent",
        "provider_session_id": "provider-session-1",
    }
    invocation_fact = next(
        fact for fact in facts if fact.event_type == "delegation.invocation_event"
    )
    assert invocation_fact.event_id == (
        "delegation:delegation-session-log:activation:1:invocation-event:1"
    )
    assert invocation_fact.payload == {
        "delegation_id": request.delegation_id,
        "invocation_id": "delegation-session-log:invocation:1",
        "activation_id": "delegation-session-log:activation:1",
        "activation_number": 1,
        "sequence": 1,
        "status": "running",
        "payload": {
            "provider_id": "fake-agent",
            "provider_session_id": "provider-session-1",
            "provider_operation_id": "operation-1",
        },
    }

    duplicate = await runtime.submit(request)
    assert await duplicate.wait() == report
    assert await session_log.read(session_id) == facts
    await runtime.stop()


@pytest.mark.asyncio
async def test_continuable_session_restores_provider_binding_after_runtime_rebuild(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "sessions.jsonl"
    store = MemoryDelegationStore()
    channels = MemoryInteractionChannelStore()
    first_port = _ControllableExecutionPort(("provider-session-1",))
    first_runtime = DelegationRuntime(
        first_port,
        channels,
        store=store,
        session_log=JsonlSessionLog(JsonlEventLog(state_path)),
        composition_id="test-composition",
    )
    first = await first_runtime.submit(
        _request("delegation-session-rebuild", mode=DelegationMode.CONTINUABLE)
    )
    first_port.handles[0].finish()
    await first.wait()
    first_snapshot = await first.snapshot()
    await first_runtime.stop()

    second_port = _ControllableExecutionPort(("provider-session-1",))
    second_runtime = DelegationRuntime(
        second_port,
        channels,
        store=store,
        session_log=JsonlSessionLog(JsonlEventLog(state_path)),
        composition_id="test-composition",
    )
    follow_up = await second_runtime.continue_request(
        ContinuationRequest(
            request_id="follow-up-after-rebuild",
            delegation_id=first.delegation_id,
            operation=ContinuationOperation.FOLLOW_UP,
            actor=_principal(),
            idempotency_key="follow-up-after-rebuild",
            session_id=first_snapshot.ref.session_id,
            message_id="follow-up-message-after-rebuild",
            expected_activation_id=first_snapshot.report_history[-1].source_activation_id,
            input={"prompt": "continue after rebuild"},
        )
    )

    assert second_port.requests[0].session_ref == SessionRef(
        provider="fake-agent",
        native_id="provider-session-1",
    )
    second_port.handles[0].finish()
    second_report = await follow_up.wait()
    assert second_report.status is DelegationStatus.COMPLETED
    assert (await follow_up.snapshot()).activation_count == 2
    await second_runtime.stop()


@pytest.mark.asyncio
async def test_provider_session_binding_conflict_requires_reconciliation(
    tmp_path: Path,
) -> None:
    port = _ControllableExecutionPort(("provider-session-1", "provider-session-2"))
    runtime = DelegationRuntime(
        port,
        MemoryInteractionChannelStore(),
        session_log=JsonlSessionLog(JsonlEventLog(tmp_path / "sessions.jsonl")),
        composition_id="test-composition",
    )
    handle = await runtime.submit(
        _request("delegation-session-conflict", mode=DelegationMode.CONTINUABLE)
    )
    port.handles[0].finish()
    await handle.wait()
    snapshot = await handle.snapshot()

    follow_up = await handle.continue_request(
        ContinuationRequest(
            request_id="conflicting-session",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.FOLLOW_UP,
            actor=_principal(),
            idempotency_key="conflicting-session",
            session_id=snapshot.ref.session_id,
            message_id="conflicting-session-message",
            expected_activation_id=snapshot.report_history[-1].source_activation_id,
            input={"prompt": "continue with another provider session"},
        )
    )
    report = await follow_up.wait()

    assert report.status is DelegationStatus.RECONCILIATION_REQUIRED
    assert report.error_code == "durable.event_conflict"
    await runtime.stop()


@pytest.mark.asyncio
async def test_one_shot_delegation_does_not_create_session_facts(tmp_path: Path) -> None:
    port = _ControllableExecutionPort(("provider-session-one-shot",))
    session_log = JsonlSessionLog(JsonlEventLog(tmp_path / "sessions.jsonl"))
    runtime = DelegationRuntime(
        port,
        MemoryInteractionChannelStore(),
        session_log=session_log,
        composition_id="test-composition",
    )

    handle = await runtime.submit(_request("delegation-one-shot-session"))
    port.handles[0].finish()
    await handle.wait()

    assert await session_log.list() == ()
    with pytest.raises(DurableNotFound):
        await session_log.get("delegation-session:delegation-one-shot-session")
    await runtime.stop()


@pytest.mark.asyncio
async def test_session_log_read_failure_happens_before_activation_preparing(
    tmp_path: Path,
) -> None:
    runtime = DelegationRuntime(
        _ControllableExecutionPort(),
        MemoryInteractionChannelStore(),
        session_log=_ReadFailingSessionLog(JsonlEventLog(tmp_path / "sessions.jsonl")),
        composition_id="test-composition",
    )

    handle = await runtime.submit(
        _request("delegation-session-read-failure", mode=DelegationMode.CONTINUABLE)
    )
    report = await handle.wait()
    snapshot = await handle.snapshot()

    assert report.status is DelegationStatus.FAILED
    assert report.error_code == "RuntimeError"
    assert snapshot.activation_count == 0
    assert snapshot.status is DelegationStatus.FAILED
    await runtime.stop()


@pytest.mark.asyncio
async def test_session_header_creation_only_retries_actual_create_races(
    tmp_path: Path,
) -> None:
    runtime = DelegationRuntime(
        _ControllableExecutionPort(),
        MemoryInteractionChannelStore(),
        session_log=_CreateConflictingSessionLog(JsonlEventLog(tmp_path / "sessions.jsonl")),
        composition_id="test-composition",
    )

    handle = await runtime.submit(
        _request("delegation-session-create-conflict", mode=DelegationMode.CONTINUABLE)
    )
    report = await handle.wait()
    snapshot = await handle.snapshot()

    assert report.status is DelegationStatus.FAILED
    assert report.error_code == "session.unsupported_format"
    assert snapshot.activation_count == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_persisted_provider_binding_rejects_fixed_provider_mismatch(
    tmp_path: Path,
) -> None:
    delegation_id = "delegation-provider-mismatch"
    session_id = f"delegation-session:{delegation_id}"
    session_log = JsonlSessionLog(JsonlEventLog(tmp_path / "sessions.jsonl"))
    await session_log.create(
        SessionHeader(
            session_id=session_id,
            owner_id="parent",
            scope_id=f"delegation-scope:{delegation_id}",
            composition_id="test-composition",
            metadata={"delegation_id": delegation_id},
        )
    )
    await session_log.append(
        session_id,
        f"delegation:{delegation_id}:provider-binding",
        "delegation.provider_session_bound",
        {
            "delegation_id": delegation_id,
            "provider_id": "fake-agent",
            "provider_session_id": "provider-session-1",
        },
    )
    port = _ControllableExecutionPort()
    runtime = DelegationRuntime(
        port,
        MemoryInteractionChannelStore(),
        session_log=session_log,
        composition_id="test-composition",
    )

    handle = await runtime.submit(
        _request(
            delegation_id,
            mode=DelegationMode.CONTINUABLE,
            provider_id="other-agent",
        )
    )
    report = await handle.wait()
    snapshot = await handle.snapshot()

    assert report.status is DelegationStatus.REJECTED
    assert report.error_code == "delegation.provider_session_mismatch"
    assert snapshot.activation_count == 0
    assert port.requests == []
    await runtime.stop()


@pytest.mark.asyncio
async def test_one_shot_delegation_maps_invocation_result_and_is_idempotent() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}, delay_seconds=0.05))
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
    assert report.source_invocation_id is not None
    assert report.source_activation_id is not None
    assert report.source_invocation_id != report.source_activation_id
    assert snapshot.activation_count == 1
    assert snapshot.current_invocation_id is None
    assert snapshot.current_activation_id is None
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
    assert first_snapshot.ref.child_scope is not None
    await handle.send_message(
        PrincipalRef(f"delegation:{handle.delegation_id}", PrincipalKind.AGENT),
        InteractionMessageDraft(
            message_id="previous-question",
            channel_id=first_snapshot.ref.channel_id,
            sender=PrincipalRef(f"delegation:{handle.delegation_id}", PrincipalKind.AGENT),
            recipient=_principal(),
            message_type=MessageType.QUESTION,
            payload={"question": "what next?"},
            scope=first_snapshot.ref.child_scope,
            correlation_id="corr-follow-up",
        ),
    )
    waiting_waiter = asyncio.create_task(handle.wait())
    await asyncio.sleep(0)
    assert not waiting_waiter.done()
    waiting_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_waiter
    waiting_snapshot = await handle.snapshot()
    assert waiting_snapshot.report is None

    follow_up = await handle.continue_request(
        ContinuationRequest(
            request_id="continuation-1",
            delegation_id="delegation-cont",
            operation=ContinuationOperation.FOLLOW_UP,
            actor=_principal(),
            idempotency_key="continuation-idem-1",
            session_id=first_snapshot.ref.session_id,
            message_id="follow-up-message-1",
            expected_activation_id=first_snapshot.report_history[-1].source_activation_id,
            correlation_id="corr-follow-up",
            reply_to="previous-question",
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
    follow_up_message = next(
        message for message in replay if message.message_id == "follow-up-message-1"
    )
    assert follow_up_message.correlation_id == "corr-follow-up"
    assert follow_up_message.reply_to == "previous-question"
    assert follow_up_message.delivery_status is MessageDeliveryStatus.COMPLETED
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_prepare_and_start_are_separate_idempotent_fenced_steps() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    handle = await runtime.submit(
        _request("delegation-prepare-start", mode=DelegationMode.CONTINUABLE)
    )
    await handle.wait()
    initial = await handle.snapshot()
    assert initial.ref.session_id is not None

    prepare = ContinuationRequest(
        request_id="prepare-1",
        delegation_id=handle.delegation_id,
        operation=ContinuationOperation.PREPARE,
        actor=_principal(),
        idempotency_key="prepare-key",
        session_id=initial.ref.session_id,
        input={"prompt": "prepared follow-up"},
    )
    await handle.continue_request(prepare)
    await handle.continue_request(replace(prepare, request_id="prepare-retry"))
    prepared = await handle.snapshot()

    assert prepared.status is DelegationStatus.PREPARING
    assert prepared.activation_count == 2
    assert prepared.current_activation_id is not None

    started = await handle.continue_request(
        ContinuationRequest(
            request_id="start-1",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.START,
            actor=_principal(),
            idempotency_key="start-key",
            session_id=prepared.ref.session_id,
            expected_activation_id=prepared.current_activation_id,
        )
    )
    report = await started.wait()
    completed = await started.snapshot()

    assert report.status is DelegationStatus.COMPLETED
    assert completed.activation_count == 2
    assert len(completed.report_history) == 2
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_prepared_activation_can_be_cancelled_without_starting_provider() -> None:
    invocation_runtime = InvocationRuntime()
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "ok"}))
    await invocation_runtime.register_provider("fake-agent", provider)
    runtime = DelegationRuntime(invocation_runtime, MemoryInteractionChannelStore())
    handle = await runtime.submit(
        _request("delegation-prepare-cancel", mode=DelegationMode.CONTINUABLE)
    )
    await handle.wait()
    initial = await handle.snapshot()
    starts_before_prepare = provider.starts

    await handle.continue_request(
        ContinuationRequest(
            request_id="prepare-cancel-prepare",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.PREPARE,
            actor=_principal(),
            idempotency_key="prepare-cancel-prepare-key",
            session_id=initial.ref.session_id,
            input={"prompt": "do not start"},
        )
    )
    await handle.cancel("parent", "cancel prepared activation")
    report = await handle.wait()

    assert report.status is DelegationStatus.CANCELLED
    assert provider.starts == starts_before_prepare
    await runtime.stop()
    await invocation_runtime.stop()


@pytest.mark.asyncio
async def test_start_after_prepared_runtime_loss_requires_reconciliation() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    channels = MemoryInteractionChannelStore()
    store = MemoryDelegationStore()
    runtime = DelegationRuntime(host.runtime, channels, store=store)
    await host.start()
    handle = await runtime.submit(
        _request("delegation-prepared-recovery", mode=DelegationMode.CONTINUABLE)
    )
    await handle.wait()
    initial = await handle.snapshot()
    await handle.continue_request(
        ContinuationRequest(
            request_id="recovery-prepare",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.PREPARE,
            actor=_principal(),
            idempotency_key="recovery-prepare-key",
            session_id=initial.ref.session_id,
            input={"prompt": "prepared"},
        )
    )
    prepared = await handle.snapshot()
    assert prepared.current_activation_id is not None

    recovered_runtime = DelegationRuntime(host.runtime, channels, store=store)
    recovered = await recovered_runtime.continue_request(
        ContinuationRequest(
            request_id="recovery-start",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.START,
            actor=_principal(),
            idempotency_key="recovery-start-key",
            session_id=prepared.ref.session_id,
            expected_activation_id=prepared.current_activation_id,
        )
    )
    report = await recovered.wait()

    assert report.status is DelegationStatus.RECONCILIATION_REQUIRED
    assert (await handle.snapshot()).status is DelegationStatus.RECONCILIATION_REQUIRED
    await recovered_runtime.stop()
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_recover_resumes_proposed_and_admitted_delegations_before_activation() -> None:
    invocation_runtime = InvocationRuntime()
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "recovered"}))
    await invocation_runtime.register_provider("fake-agent", provider)
    channels = MemoryInteractionChannelStore()
    store = MemoryDelegationStore()

    proposed = _request("delegation-recover-proposed", mode=DelegationMode.CONTINUABLE)
    admitted = _request("delegation-recover-admitted", mode=DelegationMode.CONTINUABLE)
    for request in (proposed, admitted):
        await store.create(
            request,
            DelegationRef(
                delegation_id=request.delegation_id,
                session_id=f"delegation-session:{request.delegation_id}",
                channel_id=f"delegation-channel:{request.delegation_id}",
                child_scope=ScopeRef(
                    f"delegation-scope:{request.delegation_id}",
                    parent_scope_id=request.scope.scope_id,
                ),
            ),
        )
    await store.record_admission(
        admitted.delegation_id,
        DelegationAdmission(allowed=True, reason="already admitted"),
    )

    runtime = DelegationRuntime(invocation_runtime, channels, store=store)
    recovered = await runtime.recover()
    reports = await asyncio.gather(
        store.wait_terminal(proposed.delegation_id),
        store.wait_terminal(admitted.delegation_id),
    )

    assert {snapshot.ref.delegation_id for snapshot in recovered} == {
        proposed.delegation_id,
        admitted.delegation_id,
    }
    assert all(report.status is DelegationStatus.COMPLETED for report in reports)
    assert provider.starts == 2
    await runtime.stop()
    await invocation_runtime.stop()


@pytest.mark.asyncio
async def test_waiting_input_can_be_cancelled_without_a_live_activation() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(host.runtime, channels)
    await host.start()

    handle = await runtime.submit(
        _request("delegation-waiting-cancel", mode=DelegationMode.CONTINUABLE)
    )
    await handle.wait()
    snapshot = await handle.snapshot()
    assert snapshot.ref.channel_id is not None
    assert snapshot.ref.child_scope is not None
    await handle.send_message(
        PrincipalRef(f"delegation:{handle.delegation_id}", PrincipalKind.AGENT),
        InteractionMessageDraft(
            message_id="cancel-question",
            channel_id=snapshot.ref.channel_id,
            sender=PrincipalRef(f"delegation:{handle.delegation_id}", PrincipalKind.AGENT),
            recipient=_principal(),
            message_type=MessageType.QUESTION,
            payload={"question": "stop?"},
            scope=snapshot.ref.child_scope,
            correlation_id="cancel-correlation",
        ),
    )

    await handle.cancel("parent", "no longer needed")
    report = await handle.wait()
    assert report.status is DelegationStatus.CANCELLED
    assert report.error_message == "no longer needed"

    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_delegation_message_api_enforces_child_scope_and_delivery_ownership() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(host.runtime, channels)
    await host.start()
    handle = await runtime.submit(
        _request("delegation-message-api", mode=DelegationMode.CONTINUABLE)
    )
    await handle.wait()
    snapshot = await handle.snapshot()
    assert snapshot.ref.channel_id is not None
    assert snapshot.ref.child_scope is not None

    message = await handle.send_message(
        _principal(),
        InteractionMessageDraft(
            message_id="message-api-1",
            channel_id=snapshot.ref.channel_id,
            sender=_principal(),
            recipient=PrincipalRef("worker", PrincipalKind.AGENT),
            message_type=MessageType.INSTRUCTION,
            payload={"text": "inspect"},
            scope=snapshot.ref.child_scope,
            correlation_id="corr-api",
        ),
    )
    assert message.delivery_status is MessageDeliveryStatus.ACCEPTED
    delivered = await handle.transition_message(
        _principal(),
        message.message_id,
        MessageDeliveryStatus.DELIVERED,
        expected_status=MessageDeliveryStatus.ACCEPTED,
    )
    processed = await handle.transition_message(
        PrincipalRef("worker", PrincipalKind.AGENT),
        message.message_id,
        MessageDeliveryStatus.PROCESSED,
        expected_status=MessageDeliveryStatus.DELIVERED,
    )
    completed = await handle.transition_message(
        _principal(),
        message.message_id,
        MessageDeliveryStatus.COMPLETED,
        expected_status=MessageDeliveryStatus.PROCESSED,
    )
    assert delivered.delivery_status is MessageDeliveryStatus.DELIVERED
    assert processed.delivery_status is MessageDeliveryStatus.PROCESSED
    assert completed.delivery_status is MessageDeliveryStatus.COMPLETED
    child_message = await handle.send_message(
        PrincipalRef(f"delegation:{handle.delegation_id}", PrincipalKind.AGENT),
        InteractionMessageDraft(
            message_id="message-api-child",
            channel_id=snapshot.ref.channel_id,
            sender=PrincipalRef(f"delegation:{handle.delegation_id}", PrincipalKind.AGENT),
            recipient=_principal(),
            message_type=MessageType.QUESTION,
            payload={"question": "need clarification"},
            scope=snapshot.ref.child_scope,
            correlation_id="corr-question",
        ),
    )
    assert child_message.sender.principal_id == f"delegation:{handle.delegation_id}"
    waiting_snapshot = await handle.snapshot()
    assert waiting_snapshot.status is DelegationStatus.WAITING_INPUT
    reply = await handle.continue_request(
        ContinuationRequest(
            request_id="message-api-reply",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.REPLY,
            actor=_principal(),
            idempotency_key="message-api-reply-key",
            session_id=waiting_snapshot.ref.session_id,
            message_id="message-api-answer",
            expected_activation_id=waiting_snapshot.report_history[-1].source_activation_id,
            reply_to=child_message.message_id,
            correlation_id="corr-question",
            input={"answer": "yes"},
        )
    )
    assert (await reply.wait()).status is DelegationStatus.COMPLETED
    channel_messages = await channels.read(snapshot.ref.channel_id)
    question = next(
        item for item in channel_messages if item.message_id == child_message.message_id
    )
    assert question.delivery_status is MessageDeliveryStatus.COMPLETED

    with pytest.raises(DelegationUnauthorized):
        await handle.send_message(
            _principal("intruder"),
            InteractionMessageDraft(
                message_id="message-api-forbidden",
                channel_id=snapshot.ref.channel_id,
                sender=_principal("intruder"),
                message_type=MessageType.INSTRUCTION,
                payload={"text": "escape"},
                scope=snapshot.ref.child_scope,
            ),
        )
    with pytest.raises(DelegationUnauthorized):
        await handle.send_message(
            _principal(),
            InteractionMessageDraft(
                message_id="message-api-wrong-scope",
                channel_id=snapshot.ref.channel_id,
                sender=_principal(),
                message_type=MessageType.INSTRUCTION,
                payload={"text": "escape"},
                scope=ScopeRef("outside"),
            ),
        )
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_delegation_rejects_unauthorized_and_unsupported_continuation() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}, delay_seconds=0.2))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    handle = await runtime.submit(_request("delegation-auth", mode=DelegationMode.CONTINUABLE))
    snapshot = await handle.snapshot()
    assert snapshot.current_activation_id is not None

    with pytest.raises(DelegationUnauthorized):
        await handle.continue_request(
            ContinuationRequest(
                request_id="bad-actor",
                delegation_id="delegation-auth",
                operation=ContinuationOperation.RECONCILE,
                actor=_principal("intruder"),
                idempotency_key="bad-actor-key",
                session_id=snapshot.ref.session_id,
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
                expected_activation_id=snapshot.current_activation_id,
                input={"text": "steer"},
            )
        )

    with pytest.raises(DelegationStateError, match="session"):
        await handle.continue_request(
            ContinuationRequest(
                request_id="cancel-wrong-session",
                delegation_id="delegation-auth",
                operation=ContinuationOperation.CANCEL,
                actor=_principal(),
                idempotency_key="cancel-wrong-session-key",
                session_id="wrong-session",
                input={"reason": "invalid session"},
            )
        )
    await handle.cancel("parent", "test completed")
    await handle.wait()
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_steer_pause_resume_ack_and_close_have_explicit_control_boundaries() -> None:
    execution_port = _ControllableExecutionPort()
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(execution_port, channels)
    handle = await runtime.submit(_request("delegation-controls", mode=DelegationMode.CONTINUABLE))
    snapshot = await handle.snapshot()
    assert snapshot.current_activation_id is not None
    activation_id = snapshot.current_activation_id
    execution = execution_port.handles[0]

    await handle.continue_request(
        ContinuationRequest(
            request_id="steer-control",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.STEER,
            actor=_principal(),
            idempotency_key="steer-control-key",
            session_id=snapshot.ref.session_id,
            message_id="steer-control-message",
            expected_activation_id=activation_id,
            input={"instruction": "focus"},
        )
    )
    assert execution.steers == [{"instruction": "focus"}]

    await handle.continue_request(
        ContinuationRequest(
            request_id="pause-control",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.PAUSE,
            actor=_principal(),
            idempotency_key="pause-control-key",
            session_id=snapshot.ref.session_id,
            expected_activation_id=activation_id,
            input={"reason": "inspect"},
        )
    )
    assert (await handle.snapshot()).status is DelegationStatus.PAUSED
    assert execution.pauses == [{"reason": "inspect"}]

    await handle.continue_request(
        ContinuationRequest(
            request_id="resume-control",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.RESUME,
            actor=_principal(),
            idempotency_key="resume-control-key",
            session_id=snapshot.ref.session_id,
            expected_activation_id=activation_id,
            input={"reason": "continue"},
        )
    )
    assert (await handle.snapshot()).status is DelegationStatus.ACTIVE
    assert execution.resumes == [{"reason": "continue"}]

    target = await handle.send_message(
        _principal(),
        InteractionMessageDraft(
            message_id="ack-target",
            channel_id=cast(str, snapshot.ref.channel_id),
            sender=_principal(),
            recipient=PrincipalRef("worker", PrincipalKind.AGENT),
            message_type=MessageType.PROGRESS,
            payload={"state": "seen"},
            scope=cast(ScopeRef, snapshot.ref.child_scope),
            correlation_id="ack-correlation",
        ),
    )
    await handle.continue_request(
        ContinuationRequest(
            request_id="ack-control",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.ACK,
            actor=_principal(),
            idempotency_key="ack-control-key",
            session_id=snapshot.ref.session_id,
            message_id="ack-message",
            reply_to=target.message_id,
            correlation_id="ack-correlation",
            input={"received": True},
        )
    )
    messages = await channels.read(cast(str, snapshot.ref.channel_id))
    assert next(
        item for item in messages if item.message_id == target.message_id
    ).delivery_status is (MessageDeliveryStatus.COMPLETED)

    execution.finish()
    report = await handle.wait()
    assert report.status is DelegationStatus.COMPLETED
    await handle.continue_request(
        ContinuationRequest(
            request_id="close-control",
            delegation_id=handle.delegation_id,
            operation=ContinuationOperation.CLOSE,
            actor=_principal(),
            idempotency_key="close-control-key",
            session_id=snapshot.ref.session_id,
        )
    )
    assert (await channels.snapshot(cast(str, snapshot.ref.channel_id))).closed
    await runtime.stop()


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
        expected_activation_id=snapshot.report_history[-1].source_activation_id,
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
        expected_activation_id=snapshot.report_history[-1].source_activation_id,
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
        expected_activation_id=snapshot.report_history[-1].source_activation_id,
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
        expected_activation_id=first_request.expected_activation_id,
        input=first_request.input,
    )
    first_handle = await runtime.continue_request(first_request)
    second_handle = await runtime.continue_request(second_request)

    assert await first_handle.wait() == await second_handle.wait()
    assert (await first_handle.snapshot()).activation_count == 2
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_follow_up_checks_activation_fence_and_budget_before_claiming() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    request = replace(
        _request("delegation-follow-up-fence", mode=DelegationMode.CONTINUABLE),
        policy=DelegationPolicy(
            budget=DelegationBudget(max_depth=1, fan_out_limit=1, max_activations=1)
        ),
    )
    handle = await runtime.submit(request)
    await handle.wait()
    snapshot = await handle.snapshot()
    assert snapshot.report is not None

    with pytest.raises(DelegationStateError, match="different activation"):
        await handle.continue_request(
            ContinuationRequest(
                request_id="stale-activation",
                delegation_id=handle.delegation_id,
                operation=ContinuationOperation.FOLLOW_UP,
                actor=_principal(),
                idempotency_key="stale-key",
                session_id=snapshot.ref.session_id,
                message_id="stale-message",
                expected_activation_id="wrong-activation",
                input={"prompt": "continue"},
            )
        )
    with pytest.raises(DelegationStateError, match="activation budget"):
        await handle.continue_request(
            ContinuationRequest(
                request_id="budget-exhausted",
                delegation_id=handle.delegation_id,
                operation=ContinuationOperation.FOLLOW_UP,
                actor=_principal(),
                idempotency_key="budget-key",
                session_id=snapshot.ref.session_id,
                message_id="budget-message",
                expected_activation_id=snapshot.report.source_activation_id,
                input={"prompt": "continue"},
            )
        )

    unfenced = ContinuationRequest(
        request_id="missing-runtime-fence",
        delegation_id=handle.delegation_id,
        operation=ContinuationOperation.FOLLOW_UP,
        actor=_principal(),
        idempotency_key="missing-runtime-fence-key",
        session_id=snapshot.ref.session_id,
        message_id="missing-runtime-fence-message",
        expected_activation_id=snapshot.report.source_activation_id,
        input={"prompt": "continue"},
    )
    object.__setattr__(unfenced, "expected_activation_id", None)
    with pytest.raises(DelegationStateError, match="requires expected_activation_id"):
        await handle.continue_request(unfenced)
    assert (await handle.snapshot()).activation_count == 1
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_decision_gate_denies_before_provider_start_and_is_durable() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "never"}))
    gate = StaticDelegationGate(
        DelegationAdmission(
            allowed=False,
            reason="human approval was not granted",
            error_code="delegation.approval_denied",
        )
    )
    runtime = DelegationRuntime(
        host.runtime,
        MemoryInteractionChannelStore(),
        gate=gate,
    )
    await host.start()
    handle = await runtime.submit(_request("delegation-gate-denied"))

    report = await handle.wait()
    snapshot = await handle.snapshot()

    assert report.status is DelegationStatus.REJECTED
    assert report.error_code == "delegation.approval_denied"
    assert snapshot.admission is not None
    assert snapshot.admission.allowed is False
    assert await host.runtime.store.list() == ()
    assert gate.evaluations == 1
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_child_delegation_has_own_scope_depth_and_reports_to_parent() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "child"}, delay_seconds=0.05))
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(host.runtime, channels)
    await host.start()

    parent_request = replace(
        _request("delegation-parent", mode=DelegationMode.CONTINUABLE),
        policy=DelegationPolicy(
            budget=DelegationBudget(max_depth=2, fan_out_limit=2, max_activations=4),
            persona="planner",
        ),
    )
    parent = await runtime.submit(parent_request)
    await asyncio.sleep(0)
    parent_snapshot = await parent.snapshot()
    assert parent_snapshot.ref.child_scope is not None
    parent_child_scope = parent_snapshot.ref.child_scope
    child_request = replace(
        _request("delegation-child"),
        parent_delegation_id=parent.delegation_id,
        policy=DelegationPolicy(
            child_scope=ScopeRef(
                "child-scope",
                parent_scope_id=parent_child_scope.scope_id,
            ),
            tool_allowlist=frozenset({"repo.read"}),
            persona="implementer",
        ),
    )
    child = await runtime.submit(child_request)
    child_report = await child.wait()
    child_snapshot = await child.snapshot()
    parent_snapshot = await parent.snapshot()

    assert child_report.status is DelegationStatus.COMPLETED
    assert child_snapshot.ref.parent_delegation_id == parent.delegation_id
    assert child_snapshot.ref.depth == 1
    assert child_snapshot.ref.child_scope == child_request.policy.child_scope
    assert parent_snapshot.child_refs == (child_snapshot.ref,)
    parent_messages = await channels.read(parent_snapshot.ref.channel_id or "")
    assert any(
        message.payload.get("delegation_id") == child.delegation_id
        and message.payload.get("status") == DelegationStatus.COMPLETED.value
        for message in parent_messages
    )
    invocation_snapshot = await host.runtime.store.snapshot(child_report.source_invocation_id or "")
    delegation_context = cast(JsonObject, invocation_snapshot.request.policy_context["delegation"])
    assert delegation_context["delegation_id"] == child.delegation_id
    assert delegation_context["depth"] == 1
    assert delegation_context["child_scope"] == "child-scope"
    assert delegation_context["tool_allowlist"] == ["repo.read"]
    assert delegation_context["tool_denylist"] == []
    assert delegation_context["persona"] == "implementer"
    policy_snapshot = cast(JsonObject, delegation_context["policy_snapshot"])
    assert policy_snapshot["persona"] == "implementer"
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_child_depth_and_scope_escape_are_durable_rejections() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    parent = await runtime.submit(
        replace(
            _request("delegation-depth-parent", mode=DelegationMode.CONTINUABLE),
            policy=DelegationPolicy(
                budget=DelegationBudget(max_depth=0, fan_out_limit=1, max_activations=2)
            ),
        )
    )
    await asyncio.sleep(0)
    parent_snapshot = await parent.snapshot()
    child = await runtime.submit(
        replace(
            _request("delegation-depth-child"),
            parent_delegation_id=parent.delegation_id,
            policy=DelegationPolicy(child_scope=ScopeRef("escape", parent_scope_id="other")),
        )
    )
    report = await child.wait()
    assert report.status is DelegationStatus.REJECTED
    assert report.error_code in {"delegation.depth_exceeded", "delegation.child_scope_escape"}
    assert len(await host.runtime.store.list()) == 1
    assert (await child.snapshot()).ref.depth == 0
    assert parent_snapshot.ref.depth == 0
    await runtime.stop()
    await host.stop()


@pytest.mark.asyncio
async def test_child_provider_failure_and_reconciliation_are_reported_to_parent() -> None:
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "parent"}))
    invocation_runtime = InvocationRuntime()
    await invocation_runtime.register_provider("fake-agent", provider)
    channels = MemoryInteractionChannelStore()
    runtime = DelegationRuntime(invocation_runtime, channels)
    parent = await runtime.submit(
        replace(_request("delegation-report-parent", mode=DelegationMode.CONTINUABLE))
    )
    await asyncio.sleep(0)
    provider.scenario = FakeAgentScenario(
        failure=FakeFailure(
            "fake.external_unknown",
            "provider start outcome is unknown",
            reconciliation_required=True,
        )
    )
    child = await runtime.submit(
        replace(
            _request("delegation-report-child"),
            parent_delegation_id=parent.delegation_id,
        )
    )

    report = await child.wait()
    parent_snapshot = await parent.snapshot()
    messages = await channels.read(parent_snapshot.ref.channel_id or "")

    assert report.status is DelegationStatus.RECONCILIATION_REQUIRED
    assert report.error_code == "fake.external_unknown"
    assert any(
        message.payload.get("delegation_id") == child.delegation_id
        and message.payload.get("status") == DelegationStatus.RECONCILIATION_REQUIRED.value
        for message in messages
    )
    await runtime.stop()
    await invocation_runtime.stop()


@pytest.mark.asyncio
async def test_parent_controller_and_fan_out_limits_are_enforced_before_child_start() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}, delay_seconds=0.05))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    parent = await runtime.submit(
        replace(
            _request("delegation-auth-parent", mode=DelegationMode.CONTINUABLE),
            policy=DelegationPolicy(
                budget=DelegationBudget(
                    max_depth=4,
                    fan_out_limit=4,
                    max_concurrent_children=1,
                    max_activations=2,
                )
            ),
        )
    )
    await asyncio.sleep(0)

    unauthorized = await runtime.submit(
        replace(
            _request("delegation-unauthorized-child"),
            parent_delegation_id=parent.delegation_id,
            initiator=_principal("intruder"),
            controller=_principal("intruder"),
        )
    )
    first, second = await asyncio.gather(
        runtime.submit(
            replace(
                _request("delegation-first-child"),
                parent_delegation_id=parent.delegation_id,
            )
        ),
        runtime.submit(
            replace(
                _request("delegation-second-child"),
                parent_delegation_id=parent.delegation_id,
            )
        ),
    )

    unauthorized_report, first_report, second_report = await asyncio.gather(
        unauthorized.wait(), first.wait(), second.wait()
    )
    assert unauthorized_report.status is DelegationStatus.REJECTED
    assert unauthorized_report.error_code == "delegation.parent_controller_required"
    child_reports = (first_report, second_report)
    assert {report.status for report in child_reports} == {
        DelegationStatus.COMPLETED,
        DelegationStatus.REJECTED,
    }
    rejected_child = next(
        report for report in child_reports if report.status is DelegationStatus.REJECTED
    )
    assert rejected_child.error_code == "delegation.concurrent_children_exceeded"
    assert len(await host.runtime.store.list()) == 2
    await runtime.stop()
    await host.stop()
