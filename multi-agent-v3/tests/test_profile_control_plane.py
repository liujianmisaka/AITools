from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from misaka_control_plane import (
    ControlPlaneConfig,
    ControlPlaneService,
    DecisionSubmission,
    EventSubmission,
    InstanceSubmission,
    JobSubmission,
    TemplateNodeSubmission,
    TemplateSubmission,
    TriggerSubmission,
    create_app,
)
from misaka_control_plane_workflow import ControlPlaneWorkflowProfile, create_dag_runner
from misaka_delegation_contracts import (
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
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_invocation_runtime import InvocationRuntime
from misaka_persistence_contracts import DurableJobStatus
from misaka_persistence_jsonl import JsonlEventLog, JsonlJobRegistry
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
    assert ("delegation.snapshot", "delegation.lifecycle") in snapshot.projection_sources
    assert ("interaction.channel", "interaction.message") in snapshot.projection_sources
    assert ("job.projection", "persistence.job.jsonl") in snapshot.projection_watermark_owners
    assert (
        "delegation.snapshot",
        "persistence.delegation.jsonl",
    ) in snapshot.projection_watermark_owners
    assert (
        "interaction.channel",
        "persistence.interaction.jsonl",
    ) in snapshot.projection_watermark_owners
    assert ("managed-service", "runtime.managed-service") in snapshot.resource_owners
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
        "/health",
        "/ready",
        "/models",
        "/delegations",
        "/delegations/{delegation_id}",
        "/delegations/{delegation_id}/messages",
        "/delegations/{delegation_id}/events",
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


def test_control_plane_delegation_routes_use_profile_gateway(tmp_path: Path) -> None:
    runtime = InvocationRuntime()
    service = ControlPlaneService(runtime, state_path=tmp_path / "http-delegation.jsonl")
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
