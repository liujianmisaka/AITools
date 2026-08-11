from __future__ import annotations

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
_FRONTEND_ROUTE_PREFIXES = ("templates", "instances", "providers", "settings")


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
        version="0.3.0",
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
    async def create_template_instance(template_id: str) -> Any:
        return await core_client.request_json(
            "POST",
            f"/api/v1/templates/{template_id}/instances",
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

    @app.post("/api/instances/{instance_id}/cancel", tags=["instances"])
    async def cancel_instance(instance_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/instances/{instance_id}/cancel"
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
