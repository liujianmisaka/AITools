from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from multi_agent_web.core_client import CoreApiError, CoreClient

_PACKAGE_DIR = Path(__file__).resolve().parent
_LEGACY_STATIC_DIR = _PACKAGE_DIR / "static"
_FRONTEND_DIST_DIR = _PACKAGE_DIR.parent / "frontend" / "dist"


def _frontend_available() -> bool:
    return (_FRONTEND_DIST_DIR / "index.html").is_file()


def _active_assets_dir() -> Path:
    frontend_assets = _FRONTEND_DIST_DIR / "assets"
    return frontend_assets if _frontend_available() and frontend_assets.is_dir() else _LEGACY_STATIC_DIR


def _static_revision() -> str:
    fingerprints: list[str] = []
    for path in sorted(_active_assets_dir().iterdir()):
        if not path.is_file():
            continue
        stat = path.stat()
        fingerprints.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
    payload = "|".join(fingerprints).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


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
        version="0.2.0",
        description="HTTP-only web client for the decoupled multi-agent core.",
        lifespan=lifespan,
    )
    app.state.core = core_client
    app.mount("/assets", StaticFiles(directory=_active_assets_dir()), name="assets")
    app.mount(
        "/legacy-assets",
        StaticFiles(directory=_LEGACY_STATIC_DIR),
        name="legacy-assets",
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
        if request.url.path.startswith("/assets/") and _frontend_available():
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

    def legacy_index() -> HTMLResponse:
        revision = _static_revision()
        content = (
            (_LEGACY_STATIC_DIR / "index.html")
            .read_text(encoding="utf-8")
            .replace("__STATIC_REVISION__", revision)
            .replace('href="/assets/', 'href="/legacy-assets/')
            .replace('src="/assets/', 'src="/legacy-assets/')
        )
        return HTMLResponse(
            content=content,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/", include_in_schema=False, response_model=None)
    async def index() -> FileResponse | HTMLResponse:
        if _frontend_available():
            return FileResponse(
                _FRONTEND_DIST_DIR / "index.html",
                headers={"Cache-Control": "no-store"},
            )
        return legacy_index()

    @app.get("/legacy", include_in_schema=False)
    async def legacy() -> HTMLResponse:
        return legacy_index()

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/dev/revision", include_in_schema=False)
    async def dev_revision(response: Response) -> dict[str, str | bool]:
        response.headers["Cache-Control"] = "no-store"
        enabled = os.getenv("MULTI_AGENT_WEB_LIVE_RELOAD", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return {
            "enabled": enabled,
            "revision": _static_revision() if enabled else "",
        }

    @app.get("/api/core/health", tags=["core"])
    async def core_health() -> Any:
        return await core_client.request_json("GET", "/health")

    @app.get("/api/providers", tags=["core"])
    async def providers() -> Any:
        return await core_client.request_json("GET", "/api/v1/providers")

    @app.get("/api/workspaces", tags=["core"])
    async def workspaces() -> Any:
        return await core_client.request_json("GET", "/api/v1/workspaces")

    @app.post("/api/workflows/validate", tags=["workflows"])
    async def validate_workflow(request: Request) -> Any:
        return await core_client.request_json(
            "POST",
            "/api/v1/workflows/validate",
            json_body=await json_body(request),
        )

    @app.post("/api/runs", status_code=202, tags=["runs"])
    async def create_run(request: Request) -> Any:
        return await core_client.request_json(
            "POST",
            "/api/v1/runs",
            json_body=await json_body(request),
        )

    @app.get("/api/runs/{run_id}", tags=["runs"])
    async def get_run(run_id: str) -> Any:
        return await core_client.request_json("GET", f"/api/v1/runs/{run_id}")

    @app.get("/api/runs/{run_id}/tasks", tags=["runs"])
    async def get_tasks(run_id: str) -> Any:
        return await core_client.request_json(
            "GET", f"/api/v1/runs/{run_id}/tasks"
        )

    @app.post("/api/runs/{run_id}/cancel", tags=["runs"])
    async def cancel_run(run_id: str) -> Any:
        return await core_client.request_json(
            "POST", f"/api/v1/runs/{run_id}/cancel"
        )

    @app.get("/{frontend_path:path}", include_in_schema=False, response_model=None)
    async def frontend_route(frontend_path: str) -> FileResponse:
        if frontend_path.startswith(("api/", "assets/", "legacy-assets/")):
            raise HTTPException(status_code=404, detail="not found")
        if not _frontend_available():
            raise HTTPException(status_code=404, detail="frontend build is unavailable")
        return FileResponse(
            _FRONTEND_DIST_DIR / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
