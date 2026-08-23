from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast

import httpx
import pytest
from misaka_control_plane import (
    ControlPlaneConfig,
    ControlPlaneService,
    DecisionSubmission,
    DelegationReconcileSubmission,
    DelegationReplySubmission,
    EventSubmission,
    InstanceSubmission,
    JobSubmission,
    TemplateNodeSubmission,
    TemplateSubmission,
    TriggerSubmission,
    WorkingDirectoryPolicy,
    create_app,
)
from misaka_control_plane_workflow import ControlPlaneWorkflowProfile, create_dag_runner
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationAdmission,
    DelegationMode,
    DelegationRef,
    DelegationRequest,
    DelegationStatus,
)
from misaka_delegation_jsonl import JsonlDelegationStore
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_interaction_contracts import (
    DecisionStatus,
    InteractionMessage,
    InteractionMessageDraft,
    MessageType,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_invocation_contracts import InvocationRequest, SessionRef
from misaka_invocation_runtime import InvocationRuntime, ProviderHandle
from misaka_persistence_contracts import DurableJobStatus
from misaka_persistence_jsonl import JsonlEventLog, JsonlJobRegistry
from pydantic import ValidationError
from starlette.testclient import TestClient


def _submission(job_id: str) -> JobSubmission:
    return JobSubmission(
        job_id=job_id,
        idempotency_key=f"idem-{job_id}",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        model="fake/model",
        effort="high",
        provider_id="fake",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )


def _delegation_request(delegation_id: str) -> DelegationRequest:
    controller = PrincipalRef("control-client", PrincipalKind.APPLICATION)
    return DelegationRequest(
        delegation_id=delegation_id,
        idempotency_key=f"idem-{delegation_id}",
        initiator=controller,
        controller=controller,
        scope=ScopeRef("control-delegation-scope"),
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello from delegation gateway"},
        provider_id="fake",
        model="fake/model",
        effort="high",
        mode=DelegationMode.CONTINUABLE,
        observers=(PrincipalRef("control-observer", PrincipalKind.HUMAN),),
    )


class _SessionRecordingFakeAgentProvider(FakeAgentProvider):
    def __init__(self) -> None:
        super().__init__(FakeAgentScenario(output={"answer": "session-ok"}))
        self.requests: list[InvocationRequest] = []

    async def start(self, request: InvocationRequest) -> ProviderHandle:
        self.requests.append(request)
        handle = await super().start(request)
        identity = cast(_MutableProviderIdentity, handle)
        identity.provider_session_id = "control-provider-session"
        identity.provider_operation_id = f"operation:{request.invocation_id}"
        return handle


class _MutableProviderIdentity(Protocol):
    provider_session_id: str
    provider_operation_id: str


def test_delegation_reply_submission_requires_expected_activation_id() -> None:
    payload = {
        "request_id": "reply-1",
        "idempotency_key": "reply-key",
        "actor": {"principal_id": "control-client", "kind": "application"},
        "session_id": "session-1",
        "message_id": "answer-1",
        "input": {"answer": "yes"},
        "correlation_id": "correlation-1",
        "reply_to": "question-1",
    }

    with pytest.raises(ValidationError):
        DelegationReplySubmission.model_validate(payload)

    submission = DelegationReplySubmission.model_validate(
        {**payload, "expected_activation_id": "activation-1"}
    )
    assert submission.expected_activation_id == "activation-1"


def test_delegation_reconcile_submission_requires_session_id() -> None:
    payload = {
        "request_id": "reconcile-1",
        "idempotency_key": "reconcile-key",
        "actor": {"principal_id": "control-client", "kind": "application"},
    }

    with pytest.raises(ValidationError):
        DelegationReconcileSubmission.model_validate(payload)

    submission = DelegationReconcileSubmission.model_validate(
        {**payload, "session_id": "session-1"}
    )
    assert submission.session_id == "session-1"


async def _wait_terminal(service: ControlPlaneService, job_id: str):
    for _ in range(100):
        job = await service.get(job_id)
        if job.status in {"succeeded", "failed", "cancelled", "reconciliation_required"}:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("control-plane job did not become terminal")


def test_control_plane_profiles_declare_facts_projections_and_optional_workflow(
    tmp_path: Path,
) -> None:
    runtime = InvocationRuntime()
    service = ControlPlaneService(
        runtime,
        state_path=tmp_path / "control.jsonl",
        config=ControlPlaneConfig(profile_version="2.0.0"),
    )
    snapshot = service.composition_snapshot

    assert snapshot.profile_id == "control-plane"
    assert snapshot.profile_version == "2.0.0"
    assert "fastapi" in snapshot.transport_ids
    assert ("job.lifecycle", "persistence.job.jsonl") in snapshot.fact_owners
    assert ("decision.projection", "decision.fact") in snapshot.projection_sources
    assert ("delegation.lifecycle", "persistence.delegation.jsonl") in snapshot.fact_owners
    assert ("interaction.message", "persistence.interaction.jsonl") in snapshot.fact_owners
    assert ("session.fact", "persistence.session.jsonl") in snapshot.fact_owners
    assert ("delegation.snapshot", "delegation.lifecycle") in snapshot.projection_sources
    assert ("interaction.channel", "interaction.message") in snapshot.projection_sources
    assert ("session.projection", "session.fact") in snapshot.projection_sources
    assert ("job.projection", "persistence.job.jsonl") in snapshot.projection_watermark_owners
    assert (
        "session.projection",
        "persistence.session.jsonl",
    ) in snapshot.projection_watermark_owners
    assert (
        "delegation.snapshot",
        "persistence.delegation.jsonl",
    ) in snapshot.projection_watermark_owners
    assert (
        "interaction.channel",
        "persistence.interaction.jsonl",
    ) in snapshot.projection_watermark_owners
    assert ("managed-service", "runtime.managed-service") in snapshot.resource_owners
    assert "persistence.session.jsonl" in snapshot.module_ids
    assert ("delegation-gateway", "gateway.delegation") in snapshot.resource_owners
    assert all(module_id != "profile.control-plane-workflow" for module_id in snapshot.module_ids)

    workflow = ControlPlaneWorkflowProfile(
        runtime,
        state_path=tmp_path / "workflow.jsonl",
    )
    workflow_snapshot = workflow.composition_snapshot
    assert workflow_snapshot.profile_id == "control-plane-workflow"
    assert "profile.control-plane-workflow" in workflow_snapshot.module_ids
    assert "workflow" in workflow_snapshot.transport_ids


@pytest.mark.asyncio
async def test_control_plane_persists_job_facts_and_reuses_idempotent_submission(
    tmp_path: Path,
) -> None:
    runtime = InvocationRuntime(cancellation_timeout_seconds=0.5, shutdown_timeout_seconds=0.5)
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "ok"}))
    await runtime.register_provider("fake", provider)
    service = ControlPlaneService(runtime, state_path=tmp_path / "control.jsonl")
    await service.start()
    try:
        first = await service.submit(_submission("job-1"))
        terminal = await _wait_terminal(service, "job-1")
        duplicate = await service.submit(_submission("job-1"))
        assert first.job_id == duplicate.job_id == terminal.job_id
        assert terminal.status.value == "succeeded"
        assert terminal.result == {"answer": "ok"}
        assert provider.starts == 1
    finally:
        await service.stop()
        await runtime.stop()

    runtime = InvocationRuntime(cancellation_timeout_seconds=0.5, shutdown_timeout_seconds=0.5)
    await runtime.register_provider("fake", FakeAgentProvider())
    restored_service = ControlPlaneService(runtime, state_path=tmp_path / "control.jsonl")
    await restored_service.start()
    try:
        restored = await restored_service.get("job-1")
        assert restored.status.value == "succeeded"
    finally:
        await restored_service.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_control_plane_exposes_provider_model_catalog(tmp_path: Path) -> None:
    runtime = InvocationRuntime()
    await runtime.register_provider("fake", FakeAgentProvider())
    service = ControlPlaneService(runtime, state_path=tmp_path / "control.jsonl")
    await service.start()
    try:
        catalogs = await service.models()
        assert len(catalogs) == 1
        assert catalogs[0].provider_id == "fake"
        assert catalogs[0].models[0].model_id == "fake/model"
        assert catalogs[0].models[0].supported_efforts == ["low", "medium", "high"]
    finally:
        await service.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_control_plane_delegation_gateway_persists_and_replays_events(
    tmp_path: Path,
) -> None:
    runtime = InvocationRuntime()
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "delegated"}))
    await runtime.register_provider("fake", provider)
    service = ControlPlaneService(runtime, state_path=tmp_path / "delegation.jsonl")
    await service.start()
    request = _delegation_request("control-delegation-1")
    try:
        first = await service.create_delegation(request, request.initiator)
        second = await service.create_delegation(request, request.initiator)
        assert first.ref == second.ref
        current = first
        for _ in range(100):
            current = await service.delegation(
                request.delegation_id,
                request.controller,
            )
            if current.report is not None:
                break
            await asyncio.sleep(0.01)
        assert current.status is DelegationStatus.COMPLETED
        events = await service.delegation_events(
            request.delegation_id,
            request.observers[0],
        )
        assert events
        assert events[-1].payload["status"] == "succeeded"
    finally:
        await service.stop()
        await runtime.stop()

    restored_runtime = InvocationRuntime()
    await restored_runtime.register_provider("fake", FakeAgentProvider())
    restored = ControlPlaneService(
        restored_runtime,
        state_path=tmp_path / "delegation.jsonl",
    )
    await restored.start()
    try:
        recovered = await restored.delegation(
            request.delegation_id,
            request.controller,
        )
        assert recovered.status is DelegationStatus.COMPLETED
        replay = await restored.delegation_events(
            request.delegation_id,
            request.observers[0],
        )
        assert replay
    finally:
        await restored.stop()
        await restored_runtime.stop()


@pytest.mark.asyncio
async def test_control_plane_delegation_event_stream_replays_and_waits_for_close(
    tmp_path: Path,
) -> None:
    runtime = InvocationRuntime()
    provider = FakeAgentProvider(
        FakeAgentScenario(
            output={"answer": "streamed"},
            events=({"type": "progress", "step": 1},),
        )
    )
    await runtime.register_provider("fake", provider)
    service = ControlPlaneService(runtime, state_path=tmp_path / "stream.jsonl")
    await service.start()
    request = replace(
        _delegation_request("control-delegation-stream"),
        mode=DelegationMode.ONE_SHOT,
        channel_id="delegation-channel:control-delegation-stream",
    )
    try:
        await service.create_delegation(request, request.initiator)
        stream = await service.delegation_event_stream(
            request.delegation_id,
            request.observers[0],
        )
        events = await asyncio.wait_for(
            _collect_interaction_messages(stream),
            timeout=2.0,
        )
        assert [message.sequence for message in events] == list(range(1, len(events) + 1))
        assert events
        assert events[-1].message_type is MessageType.RESULT
    finally:
        await service.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_control_plane_event_stream_route_emits_sse_and_terminal_marker(
    tmp_path: Path,
) -> None:
    runtime = InvocationRuntime()
    await runtime.register_provider(
        "fake",
        FakeAgentProvider(
            FakeAgentScenario(
                output={"answer": "http-stream"},
                events=({"type": "progress", "step": 1},),
            )
        ),
    )
    service = ControlPlaneService(runtime, state_path=tmp_path / "http-stream.jsonl")
    await service.start()
    request = replace(
        _delegation_request("control-delegation-http-stream"),
        mode=DelegationMode.ONE_SHOT,
        channel_id="delegation-channel:control-delegation-http-stream",
    )
    try:
        await service.create_delegation(request, request.initiator)
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/delegations/{request.delegation_id}/events/stream",
                params={
                    "actor_id": request.observers[0].principal_id,
                    "actor_kind": request.observers[0].kind.value,
                },
            )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: delegation.message" in response.text
        assert "id: 1" in response.text
        assert "event: delegation.end" in response.text
    finally:
        await service.stop()
        await runtime.stop()


async def _collect_interaction_messages(
    stream: AsyncIterator[InteractionMessage],
) -> list[InteractionMessage]:
    return [message async for message in stream]


@pytest.mark.asyncio
async def test_control_plane_restart_restores_delegation_provider_session(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "delegation-session.jsonl"
    first_runtime = InvocationRuntime()
    first_provider = _SessionRecordingFakeAgentProvider()
    await first_runtime.register_provider("fake", first_provider)
    first_service = ControlPlaneService(first_runtime, state_path=state_path)
    await first_service.start()
    request = _delegation_request("control-delegation-session")
    try:
        await first_service.create_delegation(request, request.initiator)
        first_snapshot = await first_service.delegation(
            request.delegation_id,
            request.controller,
        )
        for _ in range(100):
            if first_snapshot.report is not None:
                break
            await asyncio.sleep(0.01)
            first_snapshot = await first_service.delegation(
                request.delegation_id,
                request.controller,
            )
        assert first_snapshot.status is DelegationStatus.COMPLETED
        assert first_provider.requests[0].session_ref is None
        assert first_snapshot.ref.channel_id is not None
        assert first_snapshot.ref.child_scope is not None
        question = await first_service.send_delegation_message(
            request.delegation_id,
            PrincipalRef(
                f"delegation:{request.delegation_id}",
                PrincipalKind.AGENT,
            ),
            InteractionMessageDraft(
                message_id="control-session-question",
                channel_id=first_snapshot.ref.channel_id,
                sender=PrincipalRef(
                    f"delegation:{request.delegation_id}",
                    PrincipalKind.AGENT,
                ),
                recipient=request.controller,
                message_type=MessageType.QUESTION,
                payload={"question": "continue?"},
                scope=first_snapshot.ref.child_scope,
                correlation_id="control-session-correlation",
            ),
        )
        first_snapshot = await first_service.delegation(
            request.delegation_id,
            request.controller,
        )
        assert first_snapshot.status is DelegationStatus.WAITING_INPUT
    finally:
        await first_service.stop()
        await first_runtime.stop()

    restored_runtime = InvocationRuntime()
    restored_provider = _SessionRecordingFakeAgentProvider()
    await restored_runtime.register_provider("fake", restored_provider)
    restored_service = ControlPlaneService(restored_runtime, state_path=state_path)
    await restored_service.start()
    try:
        await restored_service.reply_delegation(
            ContinuationRequest(
                request_id="control-follow-up",
                delegation_id=request.delegation_id,
                operation=ContinuationOperation.REPLY,
                actor=request.controller,
                idempotency_key="control-follow-up",
                session_id=first_snapshot.ref.session_id,
                message_id="control-follow-up-message",
                expected_activation_id=(first_snapshot.report_history[-1].source_activation_id),
                correlation_id="control-session-correlation",
                reply_to=question.message_id,
                input={"prompt": "continue after restart"},
            )
        )
        for _ in range(100):
            if restored_provider.requests:
                break
            await asyncio.sleep(0.01)

        assert restored_provider.requests[0].session_ref == SessionRef(
            provider="fake",
            native_id="control-provider-session",
        )
        restored_snapshot = await restored_service.delegation(
            request.delegation_id,
            request.controller,
        )
        for _ in range(100):
            if restored_snapshot.report is not None:
                break
            await asyncio.sleep(0.01)
            restored_snapshot = await restored_service.delegation(
                request.delegation_id,
                request.controller,
            )
        assert restored_snapshot.status is DelegationStatus.COMPLETED
        assert restored_snapshot.activation_count == 2
    finally:
        await restored_service.stop()
        await restored_runtime.stop()


@pytest.mark.asyncio
async def test_restart_fences_running_job_instead_of_resubmitting(tmp_path: Path) -> None:
    state_path = tmp_path / "control.jsonl"
    registry = JsonlJobRegistry(JsonlEventLog(state_path))
    queued, created = await registry.register(
        "job-running",
        "idem-running",
        _submission("job-running").model_dump(mode="json"),
    )
    assert created is True
    await registry.transition(
        queued.job_id,
        DurableJobStatus.RUNNING,
        expected_version=queued.version,
    )

    runtime = InvocationRuntime()
    provider = FakeAgentProvider()
    await runtime.register_provider("fake", provider)
    service = ControlPlaneService(runtime, state_path=state_path)
    await service.start()
    try:
        restored = await service.get("job-running")
        assert restored.status is DurableJobStatus.RECONCILIATION_REQUIRED
        assert restored.error_code == "control.restart_reconciliation_required"
        assert provider.starts == 0
    finally:
        await service.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_restart_fences_live_delegation_without_runtime_handle(tmp_path: Path) -> None:
    state_path = tmp_path / "delegation-running.jsonl"
    log = JsonlEventLog(state_path)
    store = JsonlDelegationStore(log)
    request = _delegation_request("delegation-running")
    ref = DelegationRef(
        delegation_id=request.delegation_id,
        session_id=f"delegation-session:{request.delegation_id}",
        channel_id=f"delegation-channel:{request.delegation_id}",
        child_scope=ScopeRef(
            f"delegation-scope:{request.delegation_id}",
            parent_scope_id=request.scope.scope_id,
        ),
    )
    await store.create(request, ref)
    await store.record_admission(
        request.delegation_id,
        DelegationAdmission(allowed=True, reason="admitted for recovery test"),
    )
    await store.begin_activation(
        request.delegation_id,
        "delegation-running:invocation:1",
        "delegation-running:activation:1",
    )
    await store.mark_activation_active(
        request.delegation_id,
        "delegation-running:invocation:1",
        "delegation-running:activation:1",
    )
    await log.close()

    runtime = InvocationRuntime()
    provider = FakeAgentProvider()
    await runtime.register_provider("fake", provider)
    service = ControlPlaneService(runtime, state_path=state_path)
    await service.start()
    try:
        recovered = await service.delegation(request.delegation_id, request.controller)
        assert recovered.status is DelegationStatus.RECONCILIATION_REQUIRED
        assert recovered.report is not None
        assert recovered.report.error_code == "delegation.recovery_activation_unavailable"
        assert recovered.report.source_activation_id == "delegation-running:activation:1"
        assert provider.starts == 0
    finally:
        await service.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_template_versions_and_instances_are_distinct_and_durable(tmp_path: Path) -> None:
    state_path = tmp_path / "control.jsonl"
    runtime = InvocationRuntime()
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "template-ok"}))
    await runtime.register_provider("fake", provider)
    service = ControlPlaneService(runtime, state_path=state_path)
    await service.start()
    try:
        template = TemplateSubmission(
            template_id="template-1",
            version=1,
            name="single agent",
            coordinator="direct",
            nodes=[
                TemplateNodeSubmission(
                    node_id="agent",
                    capability_id="agent.invocation",
                    operation="invoke",
                    input={"prompt": "hello"},
                    model="fake/model",
                    effort="high",
                    provider_id="fake",
                    output_schema={
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                )
            ],
        )
        saved = await service.create_template(template)
        instance = await service.start_instance(
            "template-1",
            None,
            InstanceSubmission(instance_id="instance-1", idempotency_key="instance-1"),
        )
        assert saved.definition.version == 1
        assert instance.template_id == "template-1"
        for _ in range(100):
            instance = await service.get_instance("instance-1")
            if instance.status in {
                DurableJobStatus.SUCCEEDED,
                DurableJobStatus.FAILED,
                DurableJobStatus.RECONCILIATION_REQUIRED,
            }:
                break
            await asyncio.sleep(0.01)
        assert instance.status is DurableJobStatus.SUCCEEDED
        assert instance.result == {"status": "succeeded", "output": {"answer": "template-ok"}}
    finally:
        await service.stop()
        await runtime.stop()

    restored_runtime = InvocationRuntime()
    await restored_runtime.register_provider("fake", FakeAgentProvider())
    restored_service = ControlPlaneService(restored_runtime, state_path=state_path)
    await restored_service.start()
    try:
        restored_template = await restored_service.template("template-1", 1)
        restored_instance = await restored_service.get_instance("instance-1")
        assert restored_template.definition.name == "single agent"
        assert restored_instance.status is DurableJobStatus.SUCCEEDED
    finally:
        await restored_service.stop()
        await restored_runtime.stop()


@pytest.mark.asyncio
async def test_dag_template_executes_nodes_in_dependency_order(tmp_path: Path) -> None:
    runtime = InvocationRuntime()
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "ok"}))
    await runtime.register_provider("fake", provider)
    service = ControlPlaneService(
        runtime,
        state_path=tmp_path / "control.jsonl",
        dag_runner=create_dag_runner(runtime),
    )
    await service.start()
    try:
        await service.create_template(
            TemplateSubmission(
                template_id="dag-template",
                version=1,
                name="two steps",
                coordinator="dag",
                nodes=[
                    TemplateNodeSubmission(
                        node_id="first",
                        capability_id="agent.invocation",
                        operation="invoke",
                        input={"prompt": "first"},
                        model="fake/model",
                        effort="high",
                        provider_id="fake",
                    ),
                    TemplateNodeSubmission(
                        node_id="second",
                        capability_id="agent.invocation",
                        operation="invoke",
                        input={"prompt": "second"},
                        model="fake/model",
                        effort="high",
                        provider_id="fake",
                        depends_on=["first"],
                    ),
                ],
            )
        )
        await service.start_instance(
            "dag-template",
            1,
            InstanceSubmission(instance_id="dag-instance", idempotency_key="dag-instance"),
        )
        instance = await service.get_instance("dag-instance")
        for _ in range(100):
            instance = await service.get_instance("dag-instance")
            if instance.status in {
                DurableJobStatus.SUCCEEDED,
                DurableJobStatus.FAILED,
                DurableJobStatus.RECONCILIATION_REQUIRED,
            }:
                break
            await asyncio.sleep(0.01)
        assert instance.status is DurableJobStatus.SUCCEEDED
        assert set(instance.result or {}) == {"first", "second"}
    finally:
        await service.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_event_trigger_creates_deterministic_instance_once(tmp_path: Path) -> None:
    runtime = InvocationRuntime()
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "event-ok"}))
    await runtime.register_provider("fake", provider)
    service = ControlPlaneService(runtime, state_path=tmp_path / "control.jsonl")
    await service.start()
    try:
        await service.create_template(
            TemplateSubmission(
                template_id="event-template",
                version=1,
                name="event handler",
                coordinator="direct",
                nodes=[
                    TemplateNodeSubmission(
                        node_id="agent",
                        capability_id="agent.invocation",
                        operation="invoke",
                        input={"prompt": "event"},
                        model="fake/model",
                        effort="high",
                        provider_id="fake",
                    )
                ],
            )
        )
        await service.create_trigger(
            TriggerSubmission(
                trigger_id="trigger-1",
                event_type="dev.test.received.v1",
                template_id="event-template",
                template_version=1,
            )
        )
        event = EventSubmission(
            event_id="event-1",
            event_type="dev.test.received.v1",
            data={"value": 1},
        )
        first = await service.publish_event(event)
        second = await service.publish_event(event)
        assert first == second == ("trigger:trigger-1:event-1",)
        instance = await service.get_instance(first[0])
        for _ in range(100):
            instance = await service.get_instance(first[0])
            if instance.status in {
                DurableJobStatus.SUCCEEDED,
                DurableJobStatus.FAILED,
                DurableJobStatus.RECONCILIATION_REQUIRED,
            }:
                break
            await asyncio.sleep(0.01)
        assert instance.status is DurableJobStatus.SUCCEEDED
        assert instance.input["event_type"] == "dev.test.received.v1"
        assert provider.starts == 1
    finally:
        await service.stop()
        await runtime.stop()


@pytest.mark.asyncio
async def test_approval_is_durable_gate_before_instance_execution(tmp_path: Path) -> None:
    runtime = InvocationRuntime()
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "approved"}))
    await runtime.register_provider("fake", provider)
    service = ControlPlaneService(runtime, state_path=tmp_path / "control.jsonl")
    await service.start()
    try:
        await service.create_template(
            TemplateSubmission(
                template_id="approval-template",
                version=1,
                name="approval gate",
                coordinator="direct",
                decision_required=True,
                nodes=[
                    TemplateNodeSubmission(
                        node_id="agent",
                        capability_id="agent.invocation",
                        operation="invoke",
                        input={"prompt": "approval"},
                        model="fake/model",
                        effort="high",
                        provider_id="fake",
                    )
                ],
            )
        )
        await service.start_instance(
            "approval-template",
            1,
            InstanceSubmission(
                instance_id="approval-instance",
                idempotency_key="approval-instance",
            ),
        )
        instance = await service.get_instance("approval-instance")
        for _ in range(100):
            instance = await service.get_instance("approval-instance")
            if instance.status is DurableJobStatus.WAITING_DECISION:
                break
            await asyncio.sleep(0.01)
        assert instance.status is DurableJobStatus.WAITING_DECISION
        assert provider.starts == 0
        decision = await service.decisions()
        assert len(decision) == 1
        pending = decision[0]
        assert pending.status is DecisionStatus.PENDING
        decided = await service.decide(
            pending.proposal.ref.proposal_id,
            pending.proposal.ref.revision,
            DecisionSubmission(
                decision="approved",
                principal_id="reviewer",
                reason="reviewed",
            ),
        )
        assert decided.fact is not None
        assert decided.fact.status is DecisionStatus.APPROVED
        for _ in range(100):
            instance = await service.get_instance("approval-instance")
            if instance.status in {
                DurableJobStatus.SUCCEEDED,
                DurableJobStatus.FAILED,
                DurableJobStatus.RECONCILIATION_REQUIRED,
            }:
                break
            await asyncio.sleep(0.01)
        assert instance.status is DurableJobStatus.SUCCEEDED
        assert provider.starts == 1
    finally:
        await service.stop()
        await runtime.stop()


def test_control_plane_app_exposes_local_profile_routes() -> None:
    runtime = InvocationRuntime()
    service = ControlPlaneService(runtime, state_path="control.jsonl")
    app = create_app(service)
    paths: set[str] = set(app.openapi()["paths"])
    assert {
        "/",
        "/health",
        "/ready",
        "/models",
        "/delegations",
        "/delegations/{delegation_id}",
        "/delegations/{delegation_id}/children",
        "/delegations/{delegation_id}/approve",
        "/delegations/{delegation_id}/messages",
        "/delegations/{delegation_id}/events",
        "/delegations/{delegation_id}/events/stream",
        "/delegations/{delegation_id}/reply",
        "/delegations/{delegation_id}/cancel",
        "/delegations/{delegation_id}/reconcile",
        "/jobs",
        "/jobs/{job_id}",
        "/jobs/{job_id}/cancel",
        "/templates",
        "/templates/{template_id}",
        "/templates/{template_id}/instances",
        "/instances",
        "/instances/{instance_id}",
        "/instances/{instance_id}/cancel",
        "/triggers",
        "/events",
        "/decisions",
        "/decisions/{proposal_id}/revisions/{revision}",
        "/decisions/{proposal_id}/revisions/{revision}/decision",
    } <= paths


def test_control_plane_root_describes_api(tmp_path: Path) -> None:
    runtime = InvocationRuntime()
    app = create_app(ControlPlaneService(runtime, state_path=tmp_path / "root.jsonl"))

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["profile"] == "control-plane"
    assert payload["links"]["delegations"] == "/delegations"


def test_control_plane_children_route_preserves_order_and_parent_authorization(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "http-children.jsonl"

    async def seed_children() -> None:
        log = JsonlEventLog(state_path)
        store = JsonlDelegationStore(log)
        parent = _delegation_request("parent")
        await store.create(parent, DelegationRef(parent.delegation_id))
        for child_id in ("child-b", "child-a"):
            child = replace(
                _delegation_request(child_id),
                parent_delegation_id=parent.delegation_id,
            )
            child_ref = DelegationRef(
                child_id,
                parent_delegation_id=parent.delegation_id,
                depth=1,
            )
            await store.create(child, child_ref)
            await store.attach_child(parent.delegation_id, child_ref)
        await log.close()

    asyncio.run(seed_children())
    service = ControlPlaneService(InvocationRuntime(), state_path=state_path)
    app = create_app(service)

    with TestClient(app) as client:
        response = client.get(
            "/delegations/parent/children",
            params={"actor_id": "control-observer", "actor_kind": "human"},
        )
        assert response.status_code == 200
        assert [item["delegation_id"] for item in response.json()] == [
            "child-b",
            "child-a",
        ]

        wrong_kind = client.get(
            "/delegations/parent/children",
            params={
                "actor_id": "control-observer",
                "actor_kind": "application",
            },
        )
        assert wrong_kind.status_code == 403

        missing = client.get(
            "/delegations/missing/children",
            params={"actor_id": "control-observer", "actor_kind": "human"},
        )
        assert missing.status_code == 404


def test_control_plane_lists_only_delegations_visible_to_actor(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "http-list.jsonl"

    async def seed_delegations() -> None:
        log = JsonlEventLog(state_path)
        store = JsonlDelegationStore(log)
        visible = _delegation_request("visible")
        private_principal = PrincipalRef(
            "private-controller",
            PrincipalKind.APPLICATION,
        )
        private = replace(
            _delegation_request("private"),
            initiator=private_principal,
            controller=private_principal,
            observers=(),
        )
        await store.create(visible, DelegationRef(visible.delegation_id))
        await store.create(private, DelegationRef(private.delegation_id))
        await log.close()

    asyncio.run(seed_delegations())
    service = ControlPlaneService(InvocationRuntime(), state_path=state_path)
    app = create_app(service)

    with TestClient(app) as client:
        response = client.get(
            "/delegations",
            params={
                "actor_id": "control-observer",
                "actor_kind": "human",
            },
        )

        assert response.status_code == 200
        assert [item["delegation_id"] for item in response.json()] == ["visible"]


def test_control_plane_delegation_routes_use_profile_gateway(tmp_path: Path) -> None:
    runtime = InvocationRuntime()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ControlPlaneService(
        runtime,
        state_path=tmp_path / "http-delegation.jsonl",
        cwd_policy=WorkingDirectoryPolicy((tmp_path,)),
    )
    app = create_app(service)
    with TestClient(app) as client:
        response = client.post(
            "/delegations",
            json={
                "actor": {
                    "principal_id": "http-client",
                    "kind": "application",
                },
                "delegation_id": "http-delegation",
                "idempotency_key": "http-delegation-idem",
                "initiator": {
                    "principal_id": "http-client",
                    "kind": "application",
                },
                "controller": {
                    "principal_id": "http-client",
                    "kind": "application",
                },
                "scope": {"scope_id": "http-scope"},
                "capability_id": "missing.capability",
                "operation": "invoke",
                "input": {},
                "cwd": str(workspace),
                "provider_id": "fake",
                "model": "fake/model",
                "effort": "high",
                "policy_context": {},
                "output_schema": None,
                "plan_hash": "a" * 64,
                "decision_ref": None,
                "mode": "continuable",
                "observers": [
                    {
                        "principal_id": "http-observer",
                        "kind": "human",
                    }
                ],
            },
        )
        assert response.status_code == 202
        assert response.json()["delegation_id"] == "http-delegation"

        observed = None
        for _ in range(100):
            observed = client.get(
                "/delegations/http-delegation",
                params={"actor_id": "http-observer", "actor_kind": "human"},
            )
            if observed.json()["status"] in {"rejected", "failed"}:
                break
            time.sleep(0.01)
        assert observed is not None
        assert observed.status_code == 200
        assert observed.json()["status"] in {"rejected", "failed"}

        forbidden = client.get(
            "/delegations/http-delegation",
            params={"actor_id": "intruder", "actor_kind": "application"},
        )
        assert forbidden.status_code == 403
