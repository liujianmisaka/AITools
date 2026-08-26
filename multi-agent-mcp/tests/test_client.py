from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from misaka_mcp_gateway.client import ControlPlaneClient, ControlPlaneError
from misaka_mcp_gateway.config import GatewayConfig


def _response(payload: object) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(payload).encode("utf-8")
    return response


def test_client_sends_actor_aware_list_request() -> None:
    config = GatewayConfig(actor_id="tool-user", actor_kind="application")
    client = ControlPlaneClient(config)

    with patch(
        "misaka_mcp_gateway.client.urllib.request.urlopen",
        return_value=_response([]),
    ) as urlopen:
        assert client.list_delegations() == []

    request = urlopen.call_args.args[0]
    assert request.full_url == (
        "http://127.0.0.1:8016/delegations?actor_id=tool-user&actor_kind=application"
    )
    assert request.method == "GET"


def test_client_requests_model_catalogs() -> None:
    config = GatewayConfig()
    client = ControlPlaneClient(config)
    catalogs: list[dict[str, Any]] = [{"provider_id": "fake", "models": []}]

    with patch(
        "misaka_mcp_gateway.client.urllib.request.urlopen",
        return_value=_response(catalogs),
    ) as urlopen:
        assert client.list_model_catalogs() == catalogs

    request = urlopen.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8016/models"
    assert request.method == "GET"


def test_client_rejects_non_list_model_catalog_response() -> None:
    client = ControlPlaneClient(GatewayConfig())

    with (
        patch(
            "misaka_mcp_gateway.client.urllib.request.urlopen",
            return_value=_response({"provider_id": "fake"}),
        ),
        pytest.raises(ControlPlaneError, match="non-list model catalog"),
    ):
        client.list_model_catalogs()


def test_client_preserves_control_plane_error_detail() -> None:
    config = GatewayConfig()
    client = ControlPlaneClient(config)
    body = io.BytesIO(json.dumps({"detail": "not authorized"}).encode("utf-8"))
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8016/delegations/one",
        403,
        "Forbidden",
        Message(),
        body,
    )

    with (
        patch(
            "misaka_mcp_gateway.client.urllib.request.urlopen",
            side_effect=error,
        ),
        pytest.raises(ControlPlaneError, match="not authorized") as captured,
    ):
        client.get_delegation("one")

    assert captured.value.status == 403


def test_client_applies_bounded_timeout_to_status_poll() -> None:
    client = ControlPlaneClient(GatewayConfig(timeout_seconds=30))

    with patch(
        "misaka_mcp_gateway.client.urllib.request.urlopen",
        return_value=_response({"delegation_id": "one", "status": "active"}),
    ) as urlopen:
        assert client.get_delegation("one", timeout_seconds=0.25)["status"] == "active"

    assert urlopen.call_args.kwargs["timeout"] == 0.25


def test_client_sends_message_dispatch_to_delegation_route() -> None:
    client = ControlPlaneClient(GatewayConfig())
    payload = {
        "dispatch_id": "dispatch-1",
        "session_id": "session-1",
        "message_id": "message-1",
    }

    with patch(
        "misaka_mcp_gateway.client.urllib.request.urlopen",
        return_value=_response({"dispatch_id": "dispatch-1", "status": "completed"}),
    ) as urlopen:
        response = client.send_delegation_message("delegation/one", payload)

    assert response["status"] == "completed"
    request = urlopen.call_args.args[0]
    assert request.full_url == (
        "http://127.0.0.1:8016/delegations/delegation%2Fone/messages/dispatch"
    )
    assert request.method == "POST"
    assert json.loads(request.data) == payload
