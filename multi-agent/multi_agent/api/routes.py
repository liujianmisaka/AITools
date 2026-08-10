from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from multi_agent.api.schemas import ApprovalDecision
from multi_agent.domain.models import ApprovalStatus, RunStatus, WorkflowDefinition
from multi_agent.orchestration.engine import WorkflowEngine

_TERMINAL_RUN_STATUSES = {
    RunStatus.succeeded.value,
    RunStatus.failed.value,
    RunStatus.cancelled.value,
    RunStatus.interrupted.value,
}


def create_router(engine: WorkflowEngine) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.post("/workflows/validate", tags=["workflows"])
    async def validate_workflow(workflow: WorkflowDefinition) -> dict[str, Any]:
        engine.validate_workflow(workflow)
        return {
            "valid": True,
            "workflow_id": workflow.id,
            "task_count": len(workflow.tasks),
        }

    @router.post("/runs", status_code=202, tags=["runs"])
    async def create_run(workflow: WorkflowDefinition) -> dict[str, Any]:
        run_id = await engine.submit(workflow)
        return engine.store.get_run(run_id)

    @router.get("/coordinator", tags=["coordination"])
    async def get_coordinator() -> dict[str, Any]:
        return {
            "name": "pi",
            "enabled": False,
            "authority": "reserved_contract_advice_only",
            "can_create_workflows": False,
            "can_submit_runs": False,
            "invocation": "not_wired",
            "execution_entrypoint": "/api/v1/runs",
        }

    @router.get("/runs/{run_id}", tags=["runs"])
    async def get_run(run_id: str) -> dict[str, Any]:
        return engine.store.get_run(run_id)

    @router.get("/runs/{run_id}/tasks", tags=["runs"])
    async def get_tasks(run_id: str) -> list[dict[str, Any]]:
        return engine.store.list_task_runs(run_id)

    @router.post("/runs/{run_id}/cancel", tags=["runs"])
    async def cancel_run(run_id: str) -> dict[str, Any]:
        return await engine.cancel_run(run_id)

    @router.get("/runs/{run_id}/approvals", tags=["approvals"])
    async def get_approvals(
        run_id: str,
        status: ApprovalStatus | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        return engine.store.list_approvals(run_id, status=status)

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

    @router.get("/runs/{run_id}/events", tags=["runs"])
    async def stream_events(
        request: Request,
        run_id: str,
        after_id: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        engine.store.get_run(run_id)
        cursor = after_id
        if last_event_id:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError:
                cursor = after_id

        async def events():
            nonlocal cursor
            while True:
                batch = engine.store.list_events(run_id, after_id=cursor)
                for event in batch:
                    cursor = event.event_id
                    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                    yield f"id: {event.event_id}\nevent: {event.kind.value}\ndata: {data}\n\n"
                run = engine.store.get_run(run_id)
                if run["status"] in _TERMINAL_RUN_STATUSES and not batch:
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
