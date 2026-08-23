from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.rest_routes import create_rest_routes
from misaka_a2a_capability import A2AAgentCard
from misaka_a2a_runtime import A2AServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from misaka_a2a_http.handler import SDKRequestHandler
from misaka_a2a_http.mappers import agent_card_to_proto

LifecycleCallback = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class A2AHttpConfig:
    public_url: str = "http://127.0.0.1:8015"
    jsonrpc_path: str = "/a2a"
    rest_prefix: str = "/a2a"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("public_url must be an absolute HTTP(S) URL")
        for name, value in {
            "jsonrpc_path": self.jsonrpc_path,
            "rest_prefix": self.rest_prefix,
        }.items():
            if not value.startswith("/"):
                raise ValueError(f"{name} must start with '/'")


def create_a2a_http_app(
    server: A2AServer,
    card: A2AAgentCard,
    *,
    config: A2AHttpConfig | None = None,
    start: LifecycleCallback | None = None,
    stop: LifecycleCallback | None = None,
) -> Starlette:
    settings = config or A2AHttpConfig()
    interface_url = f"{settings.public_url.rstrip('/')}{settings.jsonrpc_path}"
    proto_card = agent_card_to_proto(card, interface_url=interface_url)
    handler = SDKRequestHandler(server)

    async def health(request: Request) -> JSONResponse:
        del request
        return JSONResponse(
            {
                "status": "ok" if server.status.value == "active" else "unavailable",
                "a2aServer": server.status.value,
                "activeTasks": server.active_task_count,
            }
        )

    async def index(request: Request) -> JSONResponse:
        del request
        return JSONResponse(
            {
                "service": card.name,
                "description": card.description,
                "version": card.version,
                "status": server.status.value,
                "agent_id": card.agent_id,
                "links": {
                    "agent_card": "/.well-known/agent-card.json",
                    "health": "/health",
                    "jsonrpc": settings.jsonrpc_path,
                    "rest": settings.rest_prefix,
                },
            }
        )

    routes = [
        Route("/", endpoint=index, methods=["GET"]),
        Route("/health", endpoint=health, methods=["GET"]),
        *create_agent_card_routes(proto_card),
        *create_jsonrpc_routes(handler, rpc_url=settings.jsonrpc_path),
        *create_rest_routes(handler, path_prefix=settings.rest_prefix),
    ]

    @asynccontextmanager
    async def lifespan(application: Starlette) -> AsyncGenerator[None, None]:
        del application
        if start is None:
            await server.start()
        else:
            await start()
        try:
            yield
        finally:
            if stop is None:
                await server.stop()
            else:
                await stop()

    return Starlette(routes=routes, lifespan=lifespan)
