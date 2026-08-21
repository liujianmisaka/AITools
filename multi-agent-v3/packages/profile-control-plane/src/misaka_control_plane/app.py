from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from misaka_approval_capability import DecisionRecord
from misaka_persistence_contracts import DurableJob
from misaka_service_runtime import ServiceSnapshot

from misaka_control_plane.delegation_api import create_delegation_router
from misaka_control_plane.models import (
    CapabilityView,
    DecisionSubmission,
    DecisionView,
    EventDeliveryView,
    EventSubmission,
    HealthView,
    InstanceSubmission,
    InstanceView,
    JobSubmission,
    JobView,
    ModelCatalogView,
    ServiceView,
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
    app.include_router(create_delegation_router(service))

    @app.get("/health", response_model=HealthView)
    async def health() -> HealthView:  # pyright: ignore[reportUnusedFunction]
        return HealthView(status="ok", profile="control-plane")

    @app.get("/ready", response_model=HealthView)
    async def ready() -> HealthView:  # pyright: ignore[reportUnusedFunction]
        if not service.started:
            raise HTTPException(status_code=503, detail="control plane is starting")
        return HealthView(status="ready", profile="control-plane")

    @app.get("/services", response_model=list[ServiceView])
    async def list_services() -> list[ServiceView]:  # pyright: ignore[reportUnusedFunction]
        return [_service_view(service) for service in await service.services()]

    @app.get("/services/{service_id}", response_model=ServiceView)
    async def get_service(service_id: str) -> ServiceView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _service_view(await service.service(service_id))
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/services/{service_id}/start", response_model=ServiceView)
    async def start_service(  # pyright: ignore[reportUnusedFunction]
        service_id: str, epoch: int | None = Query(default=None, ge=0)
    ) -> ServiceView:
        try:
            return _service_view(await service.start_service(service_id, expected_epoch=epoch))
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/services/{service_id}/stop", response_model=ServiceView)
    async def stop_service(  # pyright: ignore[reportUnusedFunction]
        service_id: str, epoch: int | None = Query(default=None, ge=0)
    ) -> ServiceView:
        try:
            return _service_view(await service.stop_service(service_id, expected_epoch=epoch))
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            return _instance_view(await service.start_instance(template_id, version, submission))
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

    @app.get("/decisions", response_model=list[DecisionView])
    async def list_decisions() -> list[DecisionView]:  # pyright: ignore[reportUnusedFunction]
        return [_decision_view(record) for record in await service.decisions()]

    @app.get("/decisions/{proposal_id}/revisions/{revision}", response_model=DecisionView)
    async def get_decision(  # pyright: ignore[reportUnusedFunction]
        proposal_id: str, revision: int
    ) -> DecisionView:
        try:
            return _decision_view(await service.decision(proposal_id, revision))
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/decisions/{proposal_id}/revisions/{revision}/decision",
        response_model=DecisionView,
    )
    async def decide(  # pyright: ignore[reportUnusedFunction]
        proposal_id: str,
        revision: int,
        decision: DecisionSubmission,
    ) -> DecisionView:
        try:
            return _decision_view(await service.decide(proposal_id, revision, decision))
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


def _service_view(snapshot: ServiceSnapshot) -> ServiceView:
    return ServiceView(
        service_id=snapshot.service_id,
        display_name=snapshot.display_name,
        description=snapshot.description,
        category=snapshot.category,
        status=snapshot.status.value,
        controllable=snapshot.controllable,
        endpoint=snapshot.endpoint,
        pid=snapshot.pid,
        process_create_time=(
            snapshot.process_identity.create_time if snapshot.process_identity is not None else None
        ),
        epoch=snapshot.epoch,
        started_at=snapshot.started_at.isoformat() if snapshot.started_at else None,
        stopped_at=snapshot.stopped_at.isoformat() if snapshot.stopped_at else None,
        exit_code=snapshot.exit_code,
        last_error=snapshot.last_error,
        recent_output=list(snapshot.recent_output),
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


def _decision_view(record: DecisionRecord) -> DecisionView:
    return DecisionView.from_record(record)


def create_local_app(state_path: str | Path) -> FastAPI:
    from misaka_invocation_runtime import InvocationRuntime

    return create_app(ControlPlaneService(InvocationRuntime(), state_path=state_path))
