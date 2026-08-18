from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from misaka_persistence_contracts import DurableJob

from misaka_control_plane.approval_registry import ApprovalRecord
from misaka_control_plane.models import (
    ApprovalDecisionSubmission,
    ApprovalView,
    CapabilityView,
    EventDeliveryView,
    EventSubmission,
    HealthView,
    InstanceSubmission,
    InstanceView,
    JobSubmission,
    JobView,
    ModelCatalogView,
    TemplateSubmission,
    TemplateView,
    TriggerSubmission,
    TriggerView,
)
from misaka_control_plane.service import ControlPlaneService
from misaka_control_plane.template_registry import InstanceRecord, TemplateRecord
from misaka_control_plane.trigger_registry import TriggerRecord


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

    @app.get("/models", response_model=list[ModelCatalogView])
    async def list_models() -> list[ModelCatalogView]:  # pyright: ignore[reportUnusedFunction]
        try:
            return await service.models()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/templates", response_model=TemplateView, status_code=201)
    async def create_template(definition: TemplateSubmission) -> TemplateView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _template_view(await service.create_template(definition))
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/templates", response_model=list[TemplateView])
    async def list_templates() -> list[TemplateView]:  # pyright: ignore[reportUnusedFunction]
        return [_template_view(record) for record in await service.templates()]

    @app.get("/templates/{template_id}", response_model=TemplateView)
    async def get_template(  # pyright: ignore[reportUnusedFunction]
        template_id: str,
        version: int | None = Query(default=None, ge=1),
    ) -> TemplateView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _template_view(await service.template(template_id, version))
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/templates/{template_id}/instances", response_model=InstanceView, status_code=202)
    async def start_instance(  # pyright: ignore[reportUnusedFunction]
        template_id: str,
        submission: InstanceSubmission,
        version: int | None = Query(default=None, ge=1),
    ) -> InstanceView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _instance_view(
                await service.start_instance(template_id, version, submission)
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/instances", response_model=list[InstanceView])
    async def list_instances() -> list[InstanceView]:  # pyright: ignore[reportUnusedFunction]
        return [_instance_view(instance) for instance in await service.instances()]

    @app.get("/instances/{instance_id}", response_model=InstanceView)
    async def get_instance(instance_id: str) -> InstanceView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _instance_view(await service.get_instance(instance_id))
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/instances/{instance_id}/cancel", response_model=InstanceView)
    async def cancel_instance(  # pyright: ignore[reportUnusedFunction]
        instance_id: str,
        reason: str = "cancelled by user",
    ) -> InstanceView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _instance_view(await service.cancel_instance(instance_id, reason))
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/triggers", response_model=TriggerView, status_code=201)
    async def create_trigger(definition: TriggerSubmission) -> TriggerView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _trigger_view(await service.create_trigger(definition))
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/triggers", response_model=list[TriggerView])
    async def list_triggers() -> list[TriggerView]:  # pyright: ignore[reportUnusedFunction]
        return [_trigger_view(trigger) for trigger in await service.triggers()]

    @app.post("/events", response_model=EventDeliveryView, status_code=202)
    async def publish_event(event: EventSubmission) -> EventDeliveryView:  # pyright: ignore[reportUnusedFunction]
        try:
            return EventDeliveryView(
                event_id=event.event_id,
                event_type=event.event_type,
                instance_ids=list(await service.publish_event(event)),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/approvals", response_model=list[ApprovalView])
    async def list_approvals() -> list[ApprovalView]:  # pyright: ignore[reportUnusedFunction]
        return [_approval_view(approval) for approval in await service.approvals()]

    @app.get("/approvals/{approval_id}", response_model=ApprovalView)
    async def get_approval(approval_id: str) -> ApprovalView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _approval_view(await service.approval(approval_id))
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/approvals/{approval_id}/decision", response_model=ApprovalView)
    async def decide_approval(  # pyright: ignore[reportUnusedFunction]
        approval_id: str,
        decision: ApprovalDecisionSubmission,
    ) -> ApprovalView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _approval_view(await service.decide_approval(approval_id, decision))
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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


def _template_view(record: TemplateRecord) -> TemplateView:
    return TemplateView(
        **record.definition.model_dump(mode="json"),
        created_at=record.created_at.isoformat(),
    )


def _instance_view(instance: InstanceRecord) -> InstanceView:
    return InstanceView(
        instance_id=instance.instance_id,
        idempotency_key=instance.idempotency_key,
        template_id=instance.template_id,
        template_version=instance.template_version,
        status=instance.status.value,
        version=instance.version,
        input=instance.input,
        result=instance.result,
        error_code=instance.error_code,
        error_message=instance.error_message,
        created_at=instance.created_at.isoformat(),
        updated_at=instance.updated_at.isoformat(),
    )


def _trigger_view(trigger: TriggerRecord) -> TriggerView:
    return TriggerView(
        **trigger.definition.model_dump(mode="json"),
        created_at=trigger.created_at.isoformat(),
    )


def _approval_view(approval: ApprovalRecord) -> ApprovalView:
    return ApprovalView(
        approval_id=approval.approval_id,
        instance_id=approval.instance_id,
        status=approval.status,
        decision=approval.decision,
        reason=approval.reason,
        created_at=approval.created_at.isoformat(),
        decided_at=approval.decided_at.isoformat() if approval.decided_at else None,
    )


def create_local_app(state_path: str | Path) -> FastAPI:
    from misaka_invocation_runtime import InvocationRuntime

    return create_app(ControlPlaneService(InvocationRuntime(), state_path=state_path))
