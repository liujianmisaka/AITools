from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from misaka_persistence_contracts import DurableJob

from misaka_control_plane.models import CapabilityView, HealthView, JobSubmission, JobView
from misaka_control_plane.service import ControlPlaneService


def create_app(service: ControlPlaneService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="Misaka Multi-Agent V3 Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthView)
    async def health() -> HealthView:  # pyright: ignore[reportUnusedFunction]
        return HealthView(status="ok", profile="control-plane")

    @app.get("/ready", response_model=HealthView)
    async def ready() -> HealthView:  # pyright: ignore[reportUnusedFunction]
        if not service.started:
            raise HTTPException(status_code=503, detail="control plane is starting")
        return HealthView(status="ready", profile="control-plane")

    @app.post("/jobs", response_model=JobView, status_code=202)
    async def submit_job(submission: JobSubmission) -> JobView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _job_view(await service.submit(submission))
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/capabilities", response_model=list[CapabilityView])
    async def list_capabilities() -> list[CapabilityView]:  # pyright: ignore[reportUnusedFunction]
        return service.capabilities()

    @app.get("/jobs", response_model=list[JobView])
    async def list_jobs() -> list[JobView]:  # pyright: ignore[reportUnusedFunction]
        return [_job_view(job) for job in await service.list()]

    @app.get("/jobs/{job_id}", response_model=JobView)
    async def get_job(job_id: str) -> JobView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _job_view(await service.get(job_id))
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/jobs/{job_id}/cancel", response_model=JobView)
    async def cancel_job(  # pyright: ignore[reportUnusedFunction]
        job_id: str, reason: str = "cancelled by user"
    ) -> JobView:
        try:
            return _job_view(await service.cancel(job_id, reason))
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def _job_view(job: DurableJob) -> JobView:
    return JobView(
        job_id=job.job_id,
        idempotency_key=job.idempotency_key,
        status=job.status.value,
        version=job.version,
        request=job.request,
        result=job.result,
        error_code=job.error_code,
        error_message=job.error_message,
    )


def create_local_app(state_path: str | Path) -> FastAPI:
    from misaka_invocation_runtime import InvocationRuntime

    return create_app(ControlPlaneService(InvocationRuntime(), state_path=state_path))
