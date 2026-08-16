from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

type RequestHandler = Callable[[Request], Awaitable[Response]]

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class SameOriginWriteMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, allowed_origins: tuple[str, ...]) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._allowed = frozenset(_normalize_origin(item) for item in allowed_origins)

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        if request.method in _SAFE_METHODS:
            return await call_next(request)
        origin = request.headers.get("origin")
        if origin is None:
            return await call_next(request)
        try:
            normalized = _normalize_origin(origin)
        except ValueError:
            normalized = ""
        if normalized not in self._allowed:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"error": "cross_origin_write_rejected"},
            )
        return await call_next(request)


def _normalize_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid origin")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"
