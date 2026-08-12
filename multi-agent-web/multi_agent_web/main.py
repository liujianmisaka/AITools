from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from multi_agent_web.core_client import CoreApiError, CoreClient

_PACKAGE_DIR = Path(__file__).resolve().parent
_FRONTEND_DIST_DIR = _PACKAGE_DIR.parent / "frontend" / "dist"
_FRONTEND_ASSETS_DIR = _FRONTEND_DIST_DIR / "assets"
_FRONTEND_ROUTE_PREFIXES = (
    "templates",
    "instances",
    "triggers",
    "scheduled-tasks",
    "providers",
    "settings",
)


def _frontend_available() -> bool:
    return (
        (_FRONTEND_DIST_DIR / "index.html").is_file()
        and _FRONTEND_ASSETS_DIR.is_dir()
    )


def create_app(core: CoreClient | None = None) -> FastAPI:
    core_client = core or CoreClient(
        os.getenv("MULTI_AGENT_CORE_URL", "http://127.0.0.1:8010")
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await core_client.close()

    app = FastAPI(
        title="Multi-Agent Web",
        version="0.4.0",
        description="HTTP-only web client for the decoupled multi-agent core.",
        lifespan=lifespan,
    )
    app.state.core = core_client
    if _frontend_available():
        app.mount(
            "/assets",
            StaticFiles(directory=_FRONTEND_ASSETS_DIR),
            name="assets",
        )

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        if request.url.path.startswith("/assets/"):
            response.headers.setdefault(
                "Cache-Control", "public, max-age=31536000, immutable"
            )
        elif request.url.path == "/" or request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(CoreApiError)
    async def core_error_handler(
        _request: Request,
        exc: CoreApiError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code},
        )

    async def json_body(request: Request) -> Any:
        try:
            return await request.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="request body must contain valid JSON",
            ) from exc

    async def optional_json_body(request: Request) -> Any | None:
        raw = await request.body()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="request body must contain valid JSON",
            ) from exc

    @app.get("/", include_in_schema=False, response_model=None)
    async def index() -> FileResponse:
        if not _frontend_available():
            raise HTTPException(
                status_code=503,
                detail="frontend build is unavailable; run npm build first",
            )
        return FileResponse(
            _FRONTEND_DIST_DIR / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/core/health", tags=["core"])
    async def core_health() -> Any:
        return await core_client.request_json("GET", "/health")

    @app.get("/api/providers", tags=["core"])
    async def providers() -> Any:
        return await core_client.request_json("GET", "/api/v1/providers")

    @app.get("/api/workspaces", tags=["core"])
    async def workspaces() -> Any:
        return await core_client.request_json("GET", "/api/v1/workspaces")

    @app.get("/api/orchestration-models", tags=["core"])
    async def orchestration_models() -> Any:
        return await core_client.request_json(
            "GET", "/api/v1/orchestration-models"
        )

    @app.get("/api/event-source-types", tags=["triggers"])
    async def event_source_types() -> Any:
        return await core_client.request_json(
            "GET", "/api/v1/event-source-types"
        )

    @app.get("/api/event-types", tags=["events"])
    async def event_types() -> Any:
        return await core_client.request_json("GET", "/api/v1/event-types")

    @app.get("/api/schedule-types", tags=["scheduling"])
    async def schedule_types() -> Any:
        return await core_client.request_json("GET", "/api/v1/schedule-types")

    @app.get("/api/scheduled-action-types", tags=["scheduling"])
    async def scheduled_action_types() -> Any:
        return await core_client.request_json(
            "GET", "/api/v1/scheduled-action-types"
        )

    @app.post("/api/templates/validate", tags=["templates"])
    async def validate_template(request: Request) -> Any:
        return await core_client.request_json(
            "POST",
            "/api/v1/templates/validate",
            json_body=await json_body(request),
        )

    @app.post("/api/templates", status_code=201, tags=["templates"])
    async def create_template(request: Request) -> Any:
        return await core_client.request_json(
            "POST",
            "/api/v1/templates",
            json_body=await json_body(request),
        )

    @app.get("/api/templates", tags=["templates"])
    async def list_templates(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
    ) -> Any:
        params: dict[str, Any] = {
            "limit": limit,
            "include_archived": include_archived,
        }
        if cursor:
            params["cursor"] = cursor
        return await core_client.request_json(
            "GET",
            "/api/v1/templates",
            params=params,
        )

    @app.get("/api/templates/{template_id}", tags=["templates"])
    async def get_template(template_id: str) -> Any:
        return await core_client.request_json(
            "GET",
            f"/api/v1/templates/{template_id}",
        )

    @app.put("/api/templates/{template_id}", tags=["templates"])
    async def update_template(template_id: str, request: Request) -> Any:
        return await core_client.request_json(
            "PUT",
            f"/api/v1/templates/{template_id}",
            json_body=await json_body(request),
        )

    @app.delete("/api/templates/{template_id}", tags=["templates"])
    async def archive_template(template_id: str) -> Any:
        return await core_client.request_json(
            "DELETE",
            f"/api/v1/templates/{template_id}",
        )

    @app.post(
        "/api/templates/{template_id}/instances",
        status_code=202,
        tags=["instances"],
    )
    async def create_template_instance(
        template_id: str,
        request: Request,
    ) -> Any:
        return await core_client.request_json(
            "POST",
            f"/api/v1/templates/{template_id}/instances",
            json_body=await optional_json_body(request),
        )

    @app.post("/api/instances", status_code=202, tags=["instances"])
    async def create_ad_hoc_instance(request: Request) -> Any:
        return await core_client.request_json(
            "POST",
            "/api/v1/instances",
            json_body=await json_body(request),
        )

    @app.get("/api/instances", tags=["instances"])
    async def list_instances(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        return await core_client.request_json(
            "GET",
            "/api/v1/instances",
            params=params,
        )

    @app.get("/api/instances/{instance_id}", tags=["instances"])
    async def get_instance(instance_id: str) -> Any:
        return await core_client.request_json(
            "GET", f"/api/v1/instances/{instance_id}"
        )

    @app.get("/api/instances/{instance_id}/tasks", tags=["instances"])
    async def get_task_instances(instance_id: str) -> Any:
        return await core_client.request_json(
            "GET", f"/api/v1/instances/{instance_id}/tasks"
        )

    @app.get("/api/instances/{instance_id}/work-items", tags=["instances"])
    async def get_work_items(instance_id: str) -> Any:
        return await core_client.request_json(
            "GET", f"/api/v1/instances/{instance_id}/work-items"
        )

    @app.post("/api/instances/{instance_id}/cancel", tags=["instances"])
    async def cancel_instance(instance_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/instances/{instance_id}/cancel"
        )

    @app.post("/api/triggers", status_code=201, tags=["triggers"])
    async def create_trigger(request: Request) -> Any:
        return await core_client.request_json(
            "POST",
            "/api/v1/triggers",
            json_body=await json_body(request),
        )

    @app.get("/api/triggers", tags=["triggers"])
    async def list_triggers(
        include_archived: bool = Query(default=False),
    ) -> Any:
        return await core_client.request_json(
            "GET",
            "/api/v1/triggers",
            params={"include_archived": include_archived},
        )

    @app.get("/api/triggers/{binding_id}", tags=["triggers"])
    async def get_trigger(binding_id: str) -> Any:
        return await core_client.request_json(
            "GET", f"/api/v1/triggers/{binding_id}"
        )

    @app.put("/api/triggers/{binding_id}", tags=["triggers"])
    async def update_trigger(binding_id: str, request: Request) -> Any:
        return await core_client.request_json(
            "PUT",
            f"/api/v1/triggers/{binding_id}",
            json_body=await json_body(request),
        )

    @app.delete("/api/triggers/{binding_id}", tags=["triggers"])
    async def archive_trigger(binding_id: str) -> Any:
        return await core_client.request_json(
            "DELETE", f"/api/v1/triggers/{binding_id}"
        )

    @app.post("/api/triggers/{binding_id}/enable", tags=["triggers"])
    async def enable_trigger(binding_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/triggers/{binding_id}/enable"
        )

    @app.post("/api/triggers/{binding_id}/disable", tags=["triggers"])
    async def disable_trigger(binding_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/triggers/{binding_id}/disable"
        )

    @app.post("/api/triggers/{binding_id}/poll", tags=["triggers"])
    async def poll_trigger(binding_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/triggers/{binding_id}/poll"
        )

    @app.post("/api/events", status_code=202, tags=["events"])
    async def publish_event(request: Request) -> Any:
        return await core_client.request_json(
            "POST",
            "/api/v1/events",
            json_body=await json_body(request),
        )

    @app.get("/api/events", tags=["events"])
    async def list_events(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Any:
        return await core_client.request_json(
            "GET", "/api/v1/events", params={"limit": limit}
        )

    @app.get("/api/events/{event_id}", tags=["events"])
    async def get_event(event_id: str) -> Any:
        return await core_client.request_json(
            "GET", f"/api/v1/events/{event_id}"
        )

    @app.post("/api/events/{event_id}/retry", tags=["events"])
    async def retry_event(event_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/events/{event_id}/retry"
        )

    @app.post("/api/scheduled-tasks", status_code=201, tags=["scheduling"])
    async def create_scheduled_task(request: Request) -> Any:
        return await core_client.request_json(
            "POST",
            "/api/v1/scheduled-tasks",
            json_body=await json_body(request),
        )

    @app.get("/api/scheduled-tasks", tags=["scheduling"])
    async def list_scheduled_tasks(
        include_archived: bool = Query(default=False),
        enabled: bool | None = Query(default=None),
    ) -> Any:
        params: dict[str, Any] = {"include_archived": include_archived}
        if enabled is not None:
            params["enabled"] = enabled
        return await core_client.request_json(
            "GET",
            "/api/v1/scheduled-tasks",
            params=params,
        )

    @app.get("/api/scheduled-tasks/{task_id}", tags=["scheduling"])
    async def get_scheduled_task(task_id: str) -> Any:
        return await core_client.request_json(
            "GET", f"/api/v1/scheduled-tasks/{task_id}"
        )

    @app.put("/api/scheduled-tasks/{task_id}", tags=["scheduling"])
    async def update_scheduled_task(task_id: str, request: Request) -> Any:
        return await core_client.request_json(
            "PUT",
            f"/api/v1/scheduled-tasks/{task_id}",
            json_body=await json_body(request),
        )

    @app.delete("/api/scheduled-tasks/{task_id}", tags=["scheduling"])
    async def archive_scheduled_task(task_id: str) -> Any:
        return await core_client.request_json(
            "DELETE", f"/api/v1/scheduled-tasks/{task_id}"
        )

    @app.post("/api/scheduled-tasks/{task_id}/enable", tags=["scheduling"])
    async def enable_scheduled_task(task_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/scheduled-tasks/{task_id}/enable"
        )

    @app.post("/api/scheduled-tasks/{task_id}/disable", tags=["scheduling"])
    async def disable_scheduled_task(task_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/scheduled-tasks/{task_id}/disable"
        )

    @app.post("/api/scheduled-tasks/{task_id}/run", tags=["scheduling"])
    async def run_scheduled_task(task_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/scheduled-tasks/{task_id}/run"
        )

    @app.get("/api/scheduled-tasks/{task_id}/runs", tags=["scheduling"])
    async def list_scheduled_task_runs(
        task_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Any:
        return await core_client.request_json(
            "GET",
            f"/api/v1/scheduled-tasks/{task_id}/runs",
            params={"limit": limit},
        )

    @app.get("/{frontend_path:path}", include_in_schema=False, response_model=None)
    async def frontend_route(frontend_path: str) -> FileResponse:
        if not any(
            frontend_path == prefix or frontend_path.startswith(f"{prefix}/")
            for prefix in _FRONTEND_ROUTE_PREFIXES
        ):
            raise HTTPException(status_code=404, detail="not found")
        if not _frontend_available():
            raise HTTPException(
                status_code=503,
                detail="frontend build is unavailable; run npm build first",
            )
        return FileResponse(
            _FRONTEND_DIST_DIR / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
