from __future__ import annotations

# pyright: reportUnusedFunction=false
import asyncio
import hmac
import ipaddress
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from multi_agent_web_v2.core_client import CoreClient, CoreResponse, CoreUnavailable
from multi_agent_web_v2.security import SameOriginWriteMiddleware
from multi_agent_web_v2.settings import WebSettings
from multi_agent_web_v2.stream_hub import StreamHub, TokenBatch, TokenEvent

_SPA_PREFIXES = (
    "dashboard",
    "templates",
    "instances",
    "approvals",
    "triggers",
    "schedules",
    "catalog",
)
_FORWARDED_REQUEST_HEADERS = frozenset(
    {
        "content-type",
        "idempotency-key",
        "x-misaka-timestamp",
        "x-misaka-nonce",
        "x-misaka-signature",
        "x-misaka-event-id",
        "x-misaka-subject",
    }
)
type RequestHandler = Callable[[Request], Awaitable[Response]]


def create_public_app(
    settings: WebSettings | None = None,
    *,
    core: CoreClient | None = None,
    hub: StreamHub | None = None,
) -> FastAPI:
    resolved = settings or WebSettings()
    core_client = core or CoreClient(resolved.control_api_url)
    stream_hub = hub or StreamHub(queue_size=resolved.stream_queue_size)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.core = core_client
        app.state.stream_hub = stream_hub
        try:
            yield
        finally:
            if core is None:
                await core_client.close()

    app = FastAPI(
        title="Multi-Agent Platform V2 Web/BFF",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(resolved.allowed_hosts))
    app.add_middleware(
        SameOriginWriteMiddleware,
        allowed_origins=resolved.allowed_origins,
    )
    _install_security_headers(app)

    assets = resolved.frontend_dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.exception_handler(CoreUnavailable)
    async def core_unavailable(_: Request, exc: CoreUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "control_api_unavailable", "detail": str(exc)},
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        hub_status = await stream_hub.status()
        return {
            "status": "ok",
            "streamHub": {
                "subscribers": hub_status.subscribers,
                "trackedExecutions": hub_status.tracked_executions,
                "droppedEvents": hub_status.dropped_events,
            },
        }

    @app.get("/ready")
    async def ready() -> Response:
        response = await core_client.request("GET", "/ready")
        return _core_response(response)

    @app.get("/api/v2/stream")
    async def stream(
        request: Request,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        header_cursor = request.headers.get("last-event-id")
        cursor = after
        if header_cursor:
            try:
                cursor = max(cursor, int(header_cursor))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Last-Event-ID must be an integer",
                ) from exc
        return StreamingResponse(
            _event_stream(
                request=request,
                core=core_client,
                hub=stream_hub,
                cursor=cursor,
                poll_seconds=resolved.stream_poll_seconds,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.api_route(
        "/api/v2/{upstream_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def proxy_api(upstream_path: str, request: Request) -> Response:
        body = await _bounded_body(request, resolved.maximum_proxy_body_bytes)
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() in _FORWARDED_REQUEST_HEADERS
        }
        response = await core_client.request(
            request.method,
            f"/api/v2/{upstream_path}",
            params=tuple(request.query_params.multi_items()),
            headers=headers,
            content=body or None,
        )
        return _core_response(response)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return _frontend_file(resolved.frontend_dist)

    @app.get("/{frontend_path:path}", include_in_schema=False)
    async def frontend(frontend_path: str) -> FileResponse:
        if not any(
            frontend_path == prefix or frontend_path.startswith(f"{prefix}/")
            for prefix in _SPA_PREFIXES
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        return _frontend_file(resolved.frontend_dist)

    return app


def create_internal_app(
    settings: WebSettings | None = None,
    *,
    hub: StreamHub | None = None,
) -> FastAPI:
    resolved = settings or WebSettings()
    stream_hub = hub or StreamHub(queue_size=resolved.stream_queue_size)
    app = FastAPI(
        title="Multi-Agent Platform V2 Internal Stream Ingress",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def loopback_only(request: Request, call_next: RequestHandler) -> Response:
        client_host = request.client.host if request.client is not None else ""
        if client_host not in {"testclient", "testserver"}:
            try:
                is_loopback = ipaddress.ip_address(client_host).is_loopback
            except ValueError:
                is_loopback = False
            if not is_loopback:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"error": "loopback_required"},
                )
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, object]:
        hub_status = await stream_hub.status()
        return {
            "status": "ok",
            "subscribers": hub_status.subscribers,
            "droppedEvents": hub_status.dropped_events,
        }

    @app.post("/internal/v1/token-batches")
    async def ingest_batch(request: Request, batch: TokenBatch) -> dict[str, int]:
        expected = resolved.internal_stream_token
        if expected is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="internal stream token is not configured",
            )
        supplied = request.headers.get("x-misaka-stream-token", "")
        if not hmac.compare_digest(expected.get_secret_value(), supplied):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid internal stream token",
            )
        return {"accepted": await stream_hub.publish(batch)}

    return app


async def serve(settings: WebSettings | None = None) -> None:
    resolved = settings or WebSettings()
    if (
        resolved.public_port == resolved.internal_port
        and resolved.public_host == resolved.internal_host
    ):
        raise RuntimeError("public and internal listeners must use different sockets")
    hub = StreamHub(queue_size=resolved.stream_queue_size)
    public = uvicorn.Server(
        uvicorn.Config(
            create_public_app(resolved, hub=hub),
            host=resolved.public_host,
            port=resolved.public_port,
            log_level="info",
        )
    )
    internal = uvicorn.Server(
        uvicorn.Config(
            create_internal_app(resolved, hub=hub),
            host=resolved.internal_host,
            port=resolved.internal_port,
            log_level="info",
        )
    )
    tasks = {
        asyncio.create_task(public.serve()),
        asyncio.create_task(internal.serve()),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    public.should_exit = True
    internal.should_exit = True
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        error = task.exception()
        if error is not None:
            raise error


def run() -> None:
    asyncio.run(serve())


async def _bounded_body(request: Request, maximum_bytes: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="request payload exceeds the configured limit",
            )
    return bytes(body)


def _core_response(response: CoreResponse) -> Response:
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.content_type,
    )


def _frontend_file(frontend_dist: Path) -> FileResponse:
    index = frontend_dist / "index.html"
    if not index.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="frontend build is unavailable",
        )
    return FileResponse(index, headers={"Cache-Control": "no-store"})


def _install_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestHandler) -> Response:
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
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        elif request.url.path == "/" or request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


async def _event_stream(
    *,
    request: Request,
    core: CoreClient,
    hub: StreamHub,
    cursor: int,
    poll_seconds: float,
) -> AsyncIterator[bytes]:
    async with hub.subscribe() as queue:
        last_keepalive = asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            cursor, persistent = await _persistent_sse(core, cursor)
            for message in persistent:
                yield message
            try:
                token = await asyncio.wait_for(queue.get(), timeout=poll_seconds)
            except TimeoutError:
                now = asyncio.get_running_loop().time()
                if now - last_keepalive >= 15:
                    yield b": keepalive\n\n"
                    last_keepalive = now
                continue
            yield _token_sse(token)


async def _persistent_sse(core: CoreClient, cursor: int) -> tuple[int, tuple[bytes, ...]]:
    document = await core.request_json(
        "GET",
        "/api/v2/events",
        params=(("after", str(cursor)), ("limit", "1000")),
    )
    if not isinstance(document, list):
        raise CoreUnavailable("Control API events response is not an array")
    messages: list[bytes] = []
    for value in cast(list[object], document):
        raw = cast(dict[str, object], value) if isinstance(value, dict) else None
        if raw is None:
            raise CoreUnavailable("Control API event is not an object")
        delivery_id = raw.get("deliveryId")
        if isinstance(delivery_id, bool) or not isinstance(delivery_id, int):
            raise CoreUnavailable("Control API event has an invalid delivery ID")
        cursor = max(cursor, delivery_id)
        payload = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        messages.append(f"id: {delivery_id}\nevent: milestone\ndata: {payload}\n\n".encode())
    return cursor, tuple(messages)


def _token_sse(event: TokenEvent) -> bytes:
    payload = event.model_dump_json(by_alias=True)
    return f"event: token\ndata: {payload}\n\n".encode()


app = create_public_app()
