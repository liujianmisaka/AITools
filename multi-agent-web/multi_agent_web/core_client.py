from __future__ import annotations

from typing import Any

import httpx2


class CoreApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        code: str = "core_unavailable",
        detail: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.detail = message if detail is None else detail


class CoreClient:
    """Narrow HTTP adapter; this package never imports the orchestration core."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        normalized = base_url.rstrip("/")
        if not normalized:
            raise ValueError("core base URL cannot be empty")
        self.base_url = normalized
        self._client = httpx2.AsyncClient(
            base_url=normalized,
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                params=params,
            )
        except httpx2.RequestError as exc:
            raise CoreApiError(f"cannot reach orchestration core: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise CoreApiError(
                "orchestration core returned a non-JSON response",
                status_code=502,
                code="invalid_core_response",
            ) from exc

        if response.is_error:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            code = payload.get("code") if isinstance(payload, dict) else None
            raise CoreApiError(
                str(detail or f"orchestration core returned HTTP {response.status_code}"),
                status_code=response.status_code,
                code=str(code or "core_request_failed"),
                detail=detail,
            )
        return payload
