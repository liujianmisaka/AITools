from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from multi_agent.api.schemas import ApprovalDecision, InstanceInput
from multi_agent.domain.errors import WebhookPayloadError
from multi_agent.domain.models import (
    ApprovalStatus,
    ScheduledTaskDefinition,
    TriggerBindingDefinition,
    TriggerEventInput,
    WorkflowDefinition,
    WorkflowInstanceStatus,
)
from multi_agent.orchestration.service import OrchestrationApplicationService


_TERMINAL_INSTANCE_STATUSES = {
    WorkflowInstanceStatus.succeeded.value,
    WorkflowInstanceStatus.failed.value,
    WorkflowInstanceStatus.cancelled.value,
    WorkflowInstanceStatus.interrupted.value,
}


def create_router(service: OrchestrationApplicationService) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/orchestration-models", tags=["orchestration"])
    async def get_orchestration_models() -> list[dict[str, object]]:
        return service.describe_models()

    @router.post("/templates/validate", tags=["templates"])
    async def validate_template(workflow: WorkflowDefinition) -> dict[str, Any]:
        return service.validate_template(workflow)

    @router.post("/templates", status_code=201, tags=["templates"])
    async def create_template(workflow: WorkflowDefinition) -> dict[str, Any]:
        return service.create_template(workflow)

    @router.get("/templates", tags=["templates"])
    async def list_templates(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
    ) -> dict[str, Any]:
        return service.list_templates(
            limit=limit,
            cursor=cursor,
            include_archived=include_archived,
        )

    @router.get("/templates/{template_id}", tags=["templates"])
    async def get_template(template_id: str) -> dict[str, Any]:
        return service.get_template(template_id)

    @router.put("/templates/{template_id}", tags=["templates"])
    async def update_template(
        template_id: str,
        workflow: WorkflowDefinition,
    ) -> dict[str, Any]:
        return service.update_template(template_id, workflow)

    @router.delete("/templates/{template_id}", tags=["templates"])
    async def archive_template(template_id: str) -> dict[str, Any]:
        return service.archive_template(template_id)

    @router.post(
        "/templates/{template_id}/instances",
        status_code=202,
        tags=["instances"],
    )
    async def create_template_instance(
        template_id: str,
        request: InstanceInput | None = None,
    ) -> dict[str, Any]:
        return await service.instantiate_template(
            template_id,
            input_data={} if request is None else request.input,
        )

    @router.post("/instances", status_code=202, tags=["instances"])
    async def create_ad_hoc_instance(
        workflow: WorkflowDefinition,
    ) -> dict[str, Any]:
        return await service.submit_ad_hoc(workflow)

    @router.get("/instances", tags=["instances"])
    async def list_instances(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None),
        status: WorkflowInstanceStatus | None = Query(default=None),
    ) -> dict[str, Any]:
        return service.list_instances(
            limit=limit,
            cursor=cursor,
            status=status,
        )

    @router.get("/coordinator", tags=["coordination"])
    async def get_coordinator() -> dict[str, Any]:
        return {
            "name": "pi",
            "enabled": False,
            "authority": "reserved_contract_advice_only",
            "can_create_templates": False,
            "can_submit_instances": False,
            "invocation": "not_wired",
            "execution_entrypoint": "/api/v1/instances",
        }

    @router.get("/instances/{instance_id}", tags=["instances"])
    async def get_instance(instance_id: str) -> dict[str, Any]:
        return service.get_instance(instance_id)

    @router.get("/instances/{instance_id}/work-items", tags=["instances"])
    async def get_work_items(instance_id: str) -> list[dict[str, Any]]:
        return service.list_work_items(instance_id)

    @router.get("/instances/{instance_id}/tasks", tags=["instances"])
    async def get_dag_tasks(instance_id: str) -> list[dict[str, Any]]:
        return service.list_dag_tasks(instance_id)

    @router.post("/instances/{instance_id}/cancel", tags=["instances"])
    async def cancel_instance(instance_id: str) -> dict[str, Any]:
        return await service.cancel_instance(instance_id)

    @router.get("/instances/{instance_id}/approvals", tags=["approvals"])
    async def get_approvals(
        instance_id: str,
        status: ApprovalStatus | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        return service.list_approvals(instance_id, status=status)

    @router.post("/approvals/{approval_id}/approve", tags=["approvals"])
    async def approve(
        approval_id: str,
        decision: ApprovalDecision,
    ) -> dict[str, Any]:
        return await service.resolve_approval(
            approval_id,
            approved=True,
            decided_by=decision.decided_by,
            reason=decision.reason,
        )

    @router.post("/approvals/{approval_id}/reject", tags=["approvals"])
    async def reject(
        approval_id: str,
        decision: ApprovalDecision,
    ) -> dict[str, Any]:
        return await service.resolve_approval(
            approval_id,
            approved=False,
            decided_by=decision.decided_by,
            reason=decision.reason,
        )

    @router.get("/providers", tags=["providers"])
    async def get_providers() -> list[dict[str, object]]:
        return await service.describe_providers()

    @router.get("/workspaces", tags=["workspaces"])
    async def get_workspaces() -> dict[str, str]:
        return service.describe_workspaces()

    @router.get("/event-source-types", tags=["triggers"])
    async def get_event_source_types() -> list[dict[str, object]]:
        return service.describe_event_sources()

    @router.get("/event-types", tags=["events"])
    async def get_event_types() -> list[dict[str, Any]]:
        return service.describe_event_types()

    @router.get("/schedule-types", tags=["scheduling"])
    async def get_schedule_types() -> list[dict[str, Any]]:
        return service.describe_schedule_types()

    @router.get("/scheduled-action-types", tags=["scheduling"])
    async def get_scheduled_action_types() -> list[dict[str, Any]]:
        return service.describe_scheduled_action_types()

    @router.post("/triggers", status_code=201, tags=["triggers"])
    async def create_trigger(
        binding: TriggerBindingDefinition,
    ) -> dict[str, Any]:
        return service.create_trigger_binding(binding)

    @router.get("/triggers", tags=["triggers"])
    async def list_triggers(
        include_archived: bool = Query(default=False),
    ) -> list[dict[str, Any]]:
        return service.list_trigger_bindings(
            include_archived=include_archived
        )

    @router.get("/triggers/{binding_id}", tags=["triggers"])
    async def get_trigger(binding_id: str) -> dict[str, Any]:
        return service.get_trigger_binding(binding_id)

    @router.put("/triggers/{binding_id}", tags=["triggers"])
    async def update_trigger(
        binding_id: str,
        binding: TriggerBindingDefinition,
    ) -> dict[str, Any]:
        return service.update_trigger_binding(binding_id, binding)

    @router.delete("/triggers/{binding_id}", tags=["triggers"])
    async def archive_trigger(binding_id: str) -> dict[str, Any]:
        return service.archive_trigger_binding(binding_id)

    @router.post("/triggers/{binding_id}/enable", tags=["triggers"])
    async def enable_trigger(binding_id: str) -> dict[str, Any]:
        return service.set_trigger_binding_enabled(binding_id, True)

    @router.post("/triggers/{binding_id}/disable", tags=["triggers"])
    async def disable_trigger(binding_id: str) -> dict[str, Any]:
        return service.set_trigger_binding_enabled(binding_id, False)

    @router.post("/triggers/{binding_id}/poll", tags=["triggers"])
    async def poll_trigger(binding_id: str) -> dict[str, Any]:
        return await service.poll_trigger_binding(binding_id)

    @router.post("/events", status_code=202, tags=["events"])
    async def publish_event(event: TriggerEventInput) -> dict[str, Any]:
        return await service.publish_trigger_event(event)

    async def _read_webhook_body(
        request: Request,
        max_payload_bytes: int,
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_payload_bytes:
                raise WebhookPayloadError(
                    f"webhook payload exceeds {max_payload_bytes} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @router.post(
        "/hooks/webhook/{endpoint_key}",
        status_code=202,
        tags=["webhooks"],
    )
    async def receive_webhook(
        endpoint_key: str,
        request: Request,
    ) -> dict[str, Any]:
        max_payload_bytes = service.webhook_payload_limit(endpoint_key)
        raw_body = await _read_webhook_body(request, max_payload_bytes)
        headers = {key.lower(): value for key, value in request.headers.items()}
        client_ip = request.client.host if request.client is not None else None
        return await service.receive_webhook(
            endpoint_key,
            headers=headers,
            raw_body=raw_body,
            client_ip=client_ip,
        )

    @router.get("/events", tags=["events"])
    async def list_events(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return service.list_trigger_events(limit=limit)

    @router.get("/events/{event_id}", tags=["events"])
    async def get_event(event_id: str) -> dict[str, Any]:
        return service.get_trigger_event(event_id)

    @router.post("/events/{event_id}/retry", tags=["events"])
    async def retry_event(event_id: str) -> dict[str, Any]:
        return await service.retry_trigger_event(event_id)

    @router.get(
        "/events/outbox/dead-letter",
        tags=["events"],
    )
    @router.get(
        "/internal-event-outbox/dead-letter",
        tags=["events"],
    )
    @router.get(
        "/outbox/dead-letter",
        tags=["events"],
    )
    async def list_dead_letter_outbox(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return service.list_dead_letter_outbox(limit=limit)

    @router.post(
        "/events/outbox/dead-letter/retry",
        tags=["events"],
    )
    @router.post(
        "/internal-event-outbox/dead-letter/retry",
        tags=["events"],
    )
    @router.post(
        "/outbox/dead-letter/retry",
        tags=["events"],
    )
    async def retry_dead_letter_outbox() -> dict[str, Any]:
        return service.retry_dead_letter_outbox()

    @router.post(
        "/scheduled-tasks",
        status_code=201,
        tags=["scheduling"],
    )
    async def create_scheduled_task(
        definition: ScheduledTaskDefinition,
    ) -> dict[str, Any]:
        return service.create_scheduled_task(definition)

    @router.get("/scheduled-tasks", tags=["scheduling"])
    async def list_scheduled_tasks(
        include_archived: bool = Query(default=False),
        enabled: bool | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        return service.list_scheduled_tasks(
            include_archived=include_archived,
            enabled=enabled,
        )

    @router.get("/scheduled-tasks/{task_id}", tags=["scheduling"])
    async def get_scheduled_task(task_id: str) -> dict[str, Any]:
        return service.get_scheduled_task(task_id)

    @router.put("/scheduled-tasks/{task_id}", tags=["scheduling"])
    async def update_scheduled_task(
        task_id: str,
        definition: ScheduledTaskDefinition,
    ) -> dict[str, Any]:
        return service.update_scheduled_task(task_id, definition)

    @router.delete("/scheduled-tasks/{task_id}", tags=["scheduling"])
    async def archive_scheduled_task(task_id: str) -> dict[str, Any]:
        return service.archive_scheduled_task(task_id)

    @router.post("/scheduled-tasks/{task_id}/enable", tags=["scheduling"])
    async def enable_scheduled_task(task_id: str) -> dict[str, Any]:
        return service.set_scheduled_task_enabled(task_id, True)

    @router.post("/scheduled-tasks/{task_id}/disable", tags=["scheduling"])
    async def disable_scheduled_task(task_id: str) -> dict[str, Any]:
        return service.set_scheduled_task_enabled(task_id, False)

    @router.post("/scheduled-tasks/{task_id}/run", tags=["scheduling"])
    async def run_scheduled_task(task_id: str) -> dict[str, Any]:
        return await service.run_scheduled_task(task_id)

    @router.get("/scheduled-tasks/{task_id}/runs", tags=["scheduling"])
    async def list_scheduled_task_runs(
        task_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return service.list_scheduled_task_runs(task_id, limit=limit)

    @router.get("/instances/{instance_id}/events", tags=["instances"])
    async def stream_instance_events(
        request: Request,
        instance_id: str,
        after_id: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        service.get_instance(instance_id)
        cursor = after_id
        if last_event_id:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError:
                cursor = after_id

        async def events():
            nonlocal cursor
            while True:
                batch = service.list_instance_events(instance_id, after_id=cursor)
                for event in batch:
                    cursor = event.event_id
                    data = json.dumps(
                        event.model_dump(mode="json"), ensure_ascii=False
                    )
                    yield (
                        f"id: {event.event_id}\n"
                        f"event: {event.kind.value}\n"
                        f"data: {data}\n\n"
                    )
                instance = service.get_instance(instance_id)
                if instance["status"] in _TERMINAL_INSTANCE_STATUSES and not batch:
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.05)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
