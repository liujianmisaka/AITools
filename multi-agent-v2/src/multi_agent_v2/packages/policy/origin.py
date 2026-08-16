from __future__ import annotations

from collections.abc import Iterable
from http import HTTPStatus

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class OriginPolicyMiddleware:
    """Reject cross-origin browser writes while allowing loopback server-to-server calls."""

    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str]) -> None:
        self._app = app
        self._allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"].upper() in _SAFE_METHODS:
            await self._app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin")
        if origin is not None and origin not in self._allowed_origins:
            response = JSONResponse(
                {"detail": "cross-origin write rejected"},
                status_code=HTTPStatus.FORBIDDEN,
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)
