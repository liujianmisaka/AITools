from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from misaka_control_plane import ControlPlaneService, JobSubmission, create_app
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_runtime import InvocationRuntime


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


def test_control_plane_app_exposes_local_profile_routes() -> None:
    runtime = InvocationRuntime()
    service = ControlPlaneService(runtime, state_path="control.jsonl")
    app = create_app(service)
    paths: set[str] = set(app.openapi()["paths"])
    assert {"/health", "/ready", "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/cancel"} <= paths
