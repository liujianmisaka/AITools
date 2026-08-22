from __future__ import annotations

import asyncio
import json
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from aitools_service_manager.models import ControlPlaneServicePayload


class ControlPlaneRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


class ControlPlaneClient(Protocol):
    async def list_services(self) -> list[ControlPlaneServicePayload]: ...

    async def get_service(self, service_id: str) -> ControlPlaneServicePayload: ...

    async def start_service(
        self, service_id: str, *, expected_epoch: int
    ) -> ControlPlaneServicePayload: ...

    async def stop_service(
        self, service_id: str, *, expected_epoch: int
    ) -> ControlPlaneServicePayload: ...


class HttpControlPlaneClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 3.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def list_services(self) -> list[ControlPlaneServicePayload]:
        payload = await self._request("/services")
        if not isinstance(payload, list):
            raise ControlPlaneRequestError("Control Plane returned an invalid service list")
        items = cast(list[object], payload)
        return [ControlPlaneServicePayload.model_validate(item) for item in items]

    async def get_service(self, service_id: str) -> ControlPlaneServicePayload:
        payload = await self._request(f"/services/{quote(service_id, safe='')}")
        return ControlPlaneServicePayload.model_validate(payload)

    async def start_service(
        self, service_id: str, *, expected_epoch: int
    ) -> ControlPlaneServicePayload:
        return await self._change_state(service_id, "start", expected_epoch)

    async def stop_service(
        self, service_id: str, *, expected_epoch: int
    ) -> ControlPlaneServicePayload:
        return await self._change_state(service_id, "stop", expected_epoch)

    async def _change_state(
        self, service_id: str, action: str, expected_epoch: int
    ) -> ControlPlaneServicePayload:
        query = urlencode({"epoch": expected_epoch})
        payload = await self._request(
            f"/services/{quote(service_id, safe='')}/{action}?{query}",
            method="POST",
        )
        return ControlPlaneServicePayload.model_validate(payload)

    async def _request(self, path: str, *, method: str = "GET") -> object:
        return await asyncio.to_thread(self._request_sync, path, method)

    def _request_sync(self, path: str, method: str) -> object:
        request = Request(
            self._base_url + path,
            method=method,
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return cast(object, json.loads(response.read().decode("utf-8")))
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            raise ControlPlaneRequestError(detail, status_code=exc.code) from exc
        except (OSError, URLError) as exc:
            raise ControlPlaneRequestError(
                f"Control Plane is unavailable at {self._base_url}: {exc}"
            ) from exc


def _http_error_detail(error: HTTPError) -> str:
    try:
        payload = cast(object, json.loads(error.read().decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return str(error.reason)
    if isinstance(payload, dict):
        mapping = cast(dict[object, object], payload)
        detail = mapping.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail
    return str(error.reason)
