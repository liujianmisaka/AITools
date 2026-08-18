from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from misaka_control_plane import (
    ControlPlaneService,
    EventSubmission,
    InstanceSubmission,
    JobSubmission,
    TemplateNodeSubmission,
    TemplateSubmission,
    TriggerSubmission,
    create_app,
)
from misaka_control_plane_workflow import create_dag_runner
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_runtime import InvocationRuntime
from misaka_persistence_contracts import DurableJobStatus
from misaka_persistence_jsonl import JsonlEventLog, JsonlJobRegistry


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


async def _wait_terminal(service: ControlPlaneService, job_id: str):
    for _ in range(100):
        job = await service.get(job_id)
        if job.status in {"succeeded", "failed", "cancelled", "reconciliation_required"}:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("control-plane job did not become terminal")


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

def test_control_plane_app_exposes_local_profile_routes() -> None:
    runtime = InvocationRuntime()
    service = ControlPlaneService(runtime, state_path="control.jsonl")
    app = create_app(service)
    paths: set[str] = set(app.openapi()["paths"])
    assert {
        "/health",
        "/ready",
        "/models",
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
    } <= paths
