from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx2


class CoreUnavailable(RuntimeError):
    """The loopback Control API could not be reached."""


@dataclass(frozen=True, slots=True)
class CoreResponse:
    status_code: int
    content: bytes
    content_type: str | None


class CoreClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 30.0) -> None:
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            timeout=httpx2.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: tuple[tuple[str, str], ...] = (),
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> CoreResponse:
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                headers=headers,
                content=content,
            )
        except httpx2.HTTPError as exc:
            raise CoreUnavailable("Control API is unavailable") from exc
        return CoreResponse(
            status_code=response.status_code,
            content=response.content,
            content_type=response.headers.get("content-type"),
        )

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: tuple[tuple[str, str], ...] = (),
    ) -> object:
        response = await self.request(method, path, params=params)
        if response.status_code >= 400:
            raise CoreUnavailable(f"Control API returned HTTP {response.status_code}")
        try:
            return httpx2.Response(
                response.status_code,
                content=response.content,
                headers={"content-type": response.content_type or "application/json"},
            ).json()
        except ValueError as exc:
            raise CoreUnavailable("Control API returned invalid JSON") from exc
