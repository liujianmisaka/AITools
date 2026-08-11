from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from multi_agent.api.schemas import ApprovalDecision
from multi_agent.domain.models import (
    ApprovalStatus,
    WorkflowDefinition,
    WorkflowInstanceStatus,
)
from multi_agent.orchestration.engine import WorkflowEngine

_TERMINAL_INSTANCE_STATUSES = {
    WorkflowInstanceStatus.succeeded.value,
    WorkflowInstanceStatus.failed.value,
    WorkflowInstanceStatus.cancelled.value,
    WorkflowInstanceStatus.interrupted.value,
}


def create_router(engine: WorkflowEngine) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.post("/templates/validate", tags=["templates"])
    async def validate_template(workflow: WorkflowDefinition) -> dict[str, Any]:
        engine.validate_workflow(workflow)
        return {
            "valid": True,
            "template_id": workflow.id,
            "task_count": len(workflow.tasks),
        }

    @router.post("/templates", status_code=201, tags=["templates"])
    async def create_template(workflow: WorkflowDefinition) -> dict[str, Any]:
        engine.validate_workflow(workflow)
        return engine.store.create_template(workflow)

    @router.get("/templates", tags=["templates"])
    async def list_templates(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
    ) -> dict[str, Any]:
        return engine.store.list_templates(
            limit=limit,
            cursor=cursor,
            include_archived=include_archived,
        )

    @router.get("/templates/{template_id}", tags=["templates"])
    async def get_template(template_id: str) -> dict[str, Any]:
        return engine.store.get_template(template_id)

    @router.put("/templates/{template_id}", tags=["templates"])
    async def update_template(
        template_id: str,
        workflow: WorkflowDefinition,
    ) -> dict[str, Any]:
        engine.validate_workflow(workflow)
        return engine.store.update_template(template_id, workflow)

    @router.delete("/templates/{template_id}", tags=["templates"])
    async def archive_template(template_id: str) -> dict[str, Any]:
        return engine.store.archive_template(template_id)

    @router.post(
        "/templates/{template_id}/instances",
        status_code=202,
        tags=["instances"],
    )
    async def create_template_instance(template_id: str) -> dict[str, Any]:
        template = engine.store.get_template(template_id)
        workflow = WorkflowDefinition.model_validate(template["definition"])
        instance_id = await engine.submit(
            workflow,
            template_id=template["id"],
            template_version=template["version"],
        )
        return engine.store.get_instance(instance_id)

    @router.post("/instances", status_code=202, tags=["instances"])
    async def create_ad_hoc_instance(
        workflow: WorkflowDefinition,
    ) -> dict[str, Any]:
        instance_id = await engine.submit(workflow)
        return engine.store.get_instance(instance_id)

    @router.get("/instances", tags=["instances"])
    async def list_instances(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None),
        status: WorkflowInstanceStatus | None = Query(default=None),
    ) -> dict[str, Any]:
        return engine.store.list_instances(
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
        return engine.store.get_instance(instance_id)

    @router.get("/instances/{instance_id}/tasks", tags=["instances"])
    async def get_task_instances(instance_id: str) -> list[dict[str, Any]]:
        return engine.store.list_task_instances(instance_id)

    @router.post("/instances/{instance_id}/cancel", tags=["instances"])
    async def cancel_instance(instance_id: str) -> dict[str, Any]:
        return await engine.cancel_instance(instance_id)

    @router.get("/instances/{instance_id}/approvals", tags=["approvals"])
    async def get_approvals(
        instance_id: str,
        status: ApprovalStatus | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        return engine.store.list_approvals(instance_id, status=status)

    @router.post("/approvals/{approval_id}/approve", tags=["approvals"])
    async def approve(
        approval_id: str,
        decision: ApprovalDecision,
    ) -> dict[str, Any]:
        return await engine.resolve_approval(
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
        return await engine.resolve_approval(
            approval_id,
            approved=False,
            decided_by=decision.decided_by,
            reason=decision.reason,
        )

    @router.get("/providers", tags=["providers"])
    async def get_providers() -> list[dict[str, object]]:
        return engine.providers.describe()

    @router.get("/workspaces", tags=["workspaces"])
    async def get_workspaces() -> dict[str, str]:
        return engine.workspaces.describe()

    @router.get("/instances/{instance_id}/events", tags=["instances"])
    async def stream_events(
        request: Request,
        instance_id: str,
        after_id: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        engine.store.get_instance(instance_id)
        cursor = after_id
        if last_event_id:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError:
                cursor = after_id

        async def events():
            nonlocal cursor
            while True:
                batch = engine.store.list_events(instance_id, after_id=cursor)
                for event in batch:
                    cursor = event.event_id
                    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                    yield (
                        f"id: {event.event_id}\n"
                        f"event: {event.kind.value}\n"
                        f"data: {data}\n\n"
                    )
                instance = engine.store.get_instance(instance_id)
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
