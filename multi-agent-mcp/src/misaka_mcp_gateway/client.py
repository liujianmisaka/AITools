from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, cast

from misaka_mcp_gateway.config import GatewayConfig


class ControlPlaneError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Control Plane request failed ({status}): {detail}")
        self.status = status
        self.detail = detail


class ControlPlaneClient:
    """HTTP client that intentionally has no V3 package imports."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def create_delegation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return _object_response(
            self._request("POST", "/delegations", payload),
            "create delegation",
        )

    def list_model_catalogs(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/models")
        if not isinstance(response, list):
            raise ControlPlaneError(
                502,
                "Control Plane returned a non-list model catalog response",
            )
        return [
            _object_response(item, "list model catalogs") for item in cast(list[object], response)
        ]

    def get_delegation(
        self,
        delegation_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return _object_response(
            self._request(
                "GET",
                f"/delegations/{urllib.parse.quote(delegation_id, safe='')}",
                query=self._actor_query(),
                timeout_seconds=timeout_seconds,
            ),
            "get delegation",
        )

    def list_delegations(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/delegations", query=self._actor_query())
        if not isinstance(response, list):
            raise ControlPlaneError(
                502,
                "Control Plane returned a non-list delegation response",
            )
        delegations: list[dict[str, Any]] = []
        for item in cast(list[object], response):
            delegations.append(_object_response(item, "list delegations"))
        return delegations

    def cancel_delegation(
        self,
        delegation_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _object_response(
            self._request(
                "POST",
                f"/delegations/{urllib.parse.quote(delegation_id, safe='')}/cancel",
                payload,
            ),
            "cancel delegation",
        )

    def send_delegation_message(
        self,
        delegation_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _object_response(
            self._request(
                "POST",
                (
                    f"/delegations/{urllib.parse.quote(delegation_id, safe='')}"
                    "/messages/dispatch"
                ),
                payload,
            ),
            "send delegation message",
        )

    def resolve_delegation_reconciliation(
        self,
        delegation_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _object_response(
            self._request(
                "POST",
                (
                    f"/delegations/{urllib.parse.quote(delegation_id, safe='')}"
                    "/reconciliation/resolve"
                ),
                payload,
            ),
            "resolve delegation reconciliation",
        )

    def _actor_query(self) -> dict[str, str]:
        return {
            "actor_id": self._config.actor_id,
            "actor_kind": self._config.actor_kind,
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        query: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> object:
        url = self._config.control_plane_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=(
                    self._config.timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
            ) as response:
                raw = response.read()
                return cast(object, json.loads(raw)) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = cast(object, json.loads(raw))
                if isinstance(parsed, Mapping):
                    mapping = cast(Mapping[str, object], parsed)
                    detail = mapping.get("detail")
                    if detail is None:
                        detail = "HTTP error response"
                else:
                    detail = parsed
            except json.JSONDecodeError:
                detail = raw.decode("utf-8", errors="replace")
            raise ControlPlaneError(exc.code, str(detail)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ControlPlaneError(503, str(exc)) from exc


def _object_response(value: object, operation: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlPlaneError(
            502,
            f"Control Plane returned a non-object response for {operation}",
        )
    return {str(key): item for key, item in cast(Mapping[object, Any], value).items()}
