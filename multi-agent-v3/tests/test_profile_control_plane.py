from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from misaka_control_plane import ControlPlaneService, JobSubmission, create_app
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
    } <= paths
