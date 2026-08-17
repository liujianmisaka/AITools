from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field

from multi_agent_v2.packages.control_plane.commands import WorkflowCommandService
from multi_agent_v2.packages.control_plane.models import (
    ApprovalDecision,
    ApprovalRecord,
    CommandAccepted,
    InstanceDetail,
    InstanceRecord,
    InstanceStart,
    ProviderCatalogRecord,
    ScheduleCreate,
    ScheduleRecord,
    ScheduleUpdate,
    TemplateCreate,
    TemplateRecord,
    TemplateVersionCreate,
    TemplateVersionRecord,
    TriggerCreate,
    TriggerRecord,
    TriggerUpdate,
    WorkflowEventRecord,
    WorkflowSignal,
    WorkflowUpdate,
)
from multi_agent_v2.packages.control_plane.service import ControlPlaneService
from multi_agent_v2.packages.eventing import (
    EventIngestResult,
    WebhookPolicy,
    generic_webhook_event,
    parse_http_cloud_event,
)
from multi_agent_v2.packages.observability.health import (
    HealthReport,
    HealthService,
    ReadinessStatus,
)
from multi_agent_v2.packages.persistence import ControlPlaneRepository

router = APIRouter()
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]


@dataclass(frozen=True, slots=True)
class ControlApiDependencies:
    service: ControlPlaneService
    repository: ControlPlaneRepository
    commands: WorkflowCommandService
    webhook_policy: WebhookPolicy | None
    maximum_event_bytes: int


class LivenessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = "ok"


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(default="cancelled from local control plane", max_length=4096)


def _health_service(request: Request) -> HealthService:
    return cast(HealthService, request.app.state.health_service)


def _dependencies(request: Request) -> ControlApiDependencies:
    value = getattr(request.app.state, "control_dependencies", None)
    if not isinstance(value, ControlApiDependencies):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="control plane is not configured",
        )
    return value


@router.get("/live", response_model=LivenessResponse)
async def live() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "/ready",
    response_model=HealthReport,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthReport}},
)
async def ready(request: Request, response: Response) -> HealthReport:
    report = await _health_service(request).report()
    if report.status is ReadinessStatus.NOT_READY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


@router.get("/health/components", response_model=HealthReport)
async def component_health(request: Request) -> HealthReport:
    return await _health_service(request).report()


@router.post(
    "/api/v2/templates",
    response_model=TemplateRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_template(
    request: Request,
    command: TemplateCreate,
    idempotency_key: IdempotencyKey,
) -> TemplateRecord:
    return await _dependencies(request).service.create_template(
        command,
        idempotency_key=idempotency_key,
    )


@router.get("/api/v2/templates", response_model=list[TemplateRecord])
async def list_templates(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> tuple[TemplateRecord, ...]:
    return await _dependencies(request).service.list_templates(limit=limit)


@router.get("/api/v2/templates/{template_id}", response_model=TemplateRecord)
async def get_template(
    request: Request,
    template_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> TemplateRecord:
    return await _dependencies(request).service.get_template(template_id)


@router.post(
    "/api/v2/templates/{template_id}/versions",
    response_model=TemplateVersionRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_template_version(
    request: Request,
    command: TemplateVersionCreate,
    idempotency_key: IdempotencyKey,
    template_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> TemplateVersionRecord:
    return await _dependencies(request).service.create_template_version(
        template_id,
        command,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/api/v2/templates/{template_id}/versions",
    response_model=list[TemplateVersionRecord],
)
async def list_template_versions(
    request: Request,
    template_id: Annotated[str, Path(min_length=1, max_length=64)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> tuple[TemplateVersionRecord, ...]:
    return await _dependencies(request).service.list_template_versions(
        template_id,
        limit=limit,
    )


@router.get(
    "/api/v2/templates/{template_id}/versions/{version}",
    response_model=TemplateVersionRecord,
)
async def get_template_version(
    request: Request,
    template_id: Annotated[str, Path(min_length=1, max_length=64)],
    version: Annotated[int, Path(ge=1)],
) -> TemplateVersionRecord:
    return await _dependencies(request).service.get_template_version(template_id, version)


@router.post(
    "/api/v2/templates/{template_id}/instances",
    response_model=InstanceRecord,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_instance(
    request: Request,
    command: InstanceStart,
    idempotency_key: IdempotencyKey,
    template_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> InstanceRecord:
    return await _dependencies(request).service.start_instance(
        template_id,
        command,
        idempotency_key=idempotency_key,
    )


@router.get("/api/v2/instances", response_model=list[InstanceRecord])
async def list_instances(
    request: Request,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> tuple[InstanceRecord, ...]:
    return await _dependencies(request).service.list_instances(
        statuses=tuple(status_filter or ()),
        limit=limit,
    )


@router.get("/api/v2/instances/{instance_id}", response_model=InstanceDetail)
async def get_instance(
    request: Request,
    instance_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> InstanceDetail:
    service = _dependencies(request).service
    instance = await service.get_instance(instance_id)
    nodes = await service.list_instance_nodes(instance_id)
    approvals = await service.list_approvals(
        instance_id=instance_id,
        pending_only=False,
        limit=1000,
    )
    return InstanceDetail(instance=instance, nodes=nodes, approvals=approvals)


@router.post(
    "/api/v2/instances/{instance_id}/cancel",
    response_model=CommandAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_instance(
    request: Request,
    command: CancelRequest,
    idempotency_key: IdempotencyKey,
    instance_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> CommandAccepted:
    return await _dependencies(request).commands.cancel_instance(
        instance_id,
        command_id=idempotency_key,
        reason=command.reason,
    )


@router.post(
    "/api/v2/instances/{instance_id}/signals",
    response_model=CommandAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def signal_instance(
    request: Request,
    command: WorkflowSignal,
    idempotency_key: IdempotencyKey,
    instance_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> CommandAccepted:
    return await _dependencies(request).commands.signal_instance(
        instance_id,
        command,
        command_id=idempotency_key,
    )


@router.post(
    "/api/v2/instances/{instance_id}/updates/{update_name}",
    response_model=CommandAccepted,
)
async def update_instance(
    request: Request,
    command: WorkflowUpdate,
    idempotency_key: IdempotencyKey,
    instance_id: Annotated[str, Path(min_length=1, max_length=64)],
    update_name: Annotated[str, Path(min_length=1, max_length=256)],
) -> CommandAccepted:
    return await _dependencies(request).commands.update_instance(
        instance_id,
        update_name,
        command,
        command_id=idempotency_key,
    )


@router.get("/api/v2/approvals", response_model=list[ApprovalRecord])
async def list_approvals(
    request: Request,
    instance_id: Annotated[str | None, Query(max_length=64)] = None,
    pending_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> tuple[ApprovalRecord, ...]:
    return await _dependencies(request).service.list_approvals(
        instance_id=instance_id,
        pending_only=pending_only,
        limit=limit,
    )


@router.post(
    "/api/v2/approvals/{approval_id}/decision",
    response_model=CommandAccepted,
)
async def decide_approval(
    request: Request,
    decision: ApprovalDecision,
    idempotency_key: IdempotencyKey,
    approval_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> CommandAccepted:
    return await _dependencies(request).commands.decide_approval(
        approval_id,
        decision,
        command_id=idempotency_key,
    )


@router.post(
    "/api/v2/triggers",
    response_model=TriggerRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_trigger(
    request: Request,
    command: TriggerCreate,
    idempotency_key: IdempotencyKey,
) -> TriggerRecord:
    return await _dependencies(request).service.create_trigger(
        command,
        idempotency_key=idempotency_key,
    )


@router.put("/api/v2/triggers/{trigger_id}", response_model=TriggerRecord)
async def update_trigger(
    request: Request,
    command: TriggerUpdate,
    idempotency_key: IdempotencyKey,
    trigger_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> TriggerRecord:
    return await _dependencies(request).service.update_trigger(
        trigger_id,
        command,
        idempotency_key=idempotency_key,
    )


@router.get("/api/v2/triggers", response_model=list[TriggerRecord])
async def list_triggers(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> tuple[TriggerRecord, ...]:
    return await _dependencies(request).service.list_triggers(limit=limit)


@router.post(
    "/api/v2/schedules",
    response_model=ScheduleRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_schedule(
    request: Request,
    command: ScheduleCreate,
    idempotency_key: IdempotencyKey,
) -> ScheduleRecord:
    return await _dependencies(request).service.create_schedule(
        command,
        idempotency_key=idempotency_key,
    )


@router.put("/api/v2/schedules/{schedule_id}", response_model=ScheduleRecord)
async def update_schedule(
    request: Request,
    command: ScheduleUpdate,
    idempotency_key: IdempotencyKey,
    schedule_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> ScheduleRecord:
    return await _dependencies(request).service.update_schedule(
        schedule_id,
        command,
        idempotency_key=idempotency_key,
    )


@router.get("/api/v2/schedules", response_model=list[ScheduleRecord])
async def list_schedules(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> tuple[ScheduleRecord, ...]:
    return await _dependencies(request).service.list_schedules(limit=limit)


@router.post(
    "/api/v2/events",
    response_model=EventIngestResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_event(request: Request) -> EventIngestResult:
    dependencies = _dependencies(request)
    body = await _read_bounded_body(request, dependencies.maximum_event_bytes)
    event = parse_http_cloud_event(request.headers, body)
    return await dependencies.repository.ingest_event(event)


@router.post(
    "/api/v2/webhooks/{source_name}",
    response_model=EventIngestResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_webhook(
    request: Request,
    source_name: Annotated[str, Path(min_length=1, max_length=128)],
) -> EventIngestResult:
    dependencies = _dependencies(request)
    policy = dependencies.webhook_policy
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="generic webhook ingestion is not configured",
        )
    body = await _read_bounded_body(request, policy.maximum_body_bytes)
    await policy.verify(request.headers, body, source_name=source_name)
    event = generic_webhook_event(
        source_name=source_name,
        headers=request.headers,
        body=body,
    )
    return await dependencies.repository.ingest_event(event)


@router.get("/api/v2/events", response_model=list[WorkflowEventRecord])
async def list_events(
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    instance_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 1000,
) -> tuple[WorkflowEventRecord, ...]:
    return await _dependencies(request).service.list_events(
        after_delivery_id=after,
        instance_id=instance_id,
        limit=limit,
    )


@router.get("/api/v2/catalog/models", response_model=ProviderCatalogRecord)
async def get_provider_catalog(request: Request) -> ProviderCatalogRecord:
    return await _dependencies(request).service.get_provider_catalog()


@router.get("/api/v2/catalog/workspaces", response_model=list[str])
async def list_workspace_ids(request: Request) -> tuple[str, ...]:
    return _dependencies(request).service.list_workspace_ids()


async def _read_bounded_body(request: Request, maximum_bytes: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="request payload exceeds the configured limit",
            )
    return bytes(body)
