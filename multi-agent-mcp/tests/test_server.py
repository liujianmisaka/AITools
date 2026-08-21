from __future__ import annotations

import io
import json
from collections.abc import Mapping
from typing import Any

from misaka_mcp_gateway.config import GatewayConfig
from misaka_mcp_gateway.server import McpStdioServer


class FakeClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[tuple[str, dict[str, Any]]] = []

    def create_delegation(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized = dict(payload)
        self.created.append(normalized)
        return {
            "delegation_id": normalized["delegation_id"],
            "status": "proposed",
        }

    def get_delegation(self, delegation_id: str) -> dict[str, Any]:
        return {"delegation_id": delegation_id, "status": "active"}

    def list_delegations(self) -> list[dict[str, Any]]:
        return [
            {"delegation_id": "one", "status": "active"},
            {"delegation_id": "two", "status": "completed"},
        ]

    def cancel_delegation(
        self,
        delegation_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.cancelled.append((delegation_id, dict(payload)))
        return {"delegation_id": delegation_id, "status": "cancelled"}


def _server() -> tuple[McpStdioServer, FakeClient]:
    client = FakeClient()
    return (
        McpStdioServer(
            GatewayConfig(
                provider_id="fake",
                model="fake/model",
                effort="high",
            ),
            client,
        ),
        client,
    )


def test_initialize_and_tools_list() -> None:
    server, _ = _server()
    initialized = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    assert initialized is not None
    assert initialized["result"]["protocolVersion"] == "2025-03-26"
    assert initialized["result"]["serverInfo"]["name"] == "misaka-multi-agent-mcp"

    listed = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed is not None
    assert {tool["name"] for tool in listed["result"]["tools"]} == {
        "delegate_task",
        "get_task_status",
        "list_tasks",
        "cancel_task",
    }


def test_modern_discovery_and_tool_results_are_complete() -> None:
    server, _ = _server()
    metadata = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/client": {
            "name": "test-client",
            "version": "1.0.0",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    discovered = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "discover-1",
            "method": "server/discover",
            "params": {"_meta": metadata},
        }
    )
    assert discovered is not None
    assert discovered["result"]["resultType"] == "complete"
    assert "2026-07-28" in discovered["result"]["supportedVersions"]

    listed = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "list-1",
            "method": "tools/list",
            "params": {"_meta": metadata},
        }
    )
    assert listed is not None
    assert listed["result"]["resultType"] == "complete"

    called = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {
                "_meta": metadata,
                "name": "get_task_status",
                "arguments": {"delegation_id": "one"},
            },
        }
    )
    assert called is not None
    assert called["result"]["resultType"] == "complete"


def test_modern_request_rejects_unsupported_protocol_version() -> None:
    server, _ = _server()
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "future-1",
            "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}},
        }
    )
    assert response is not None
    assert response["error"]["code"] == -32022


def test_delegate_task_normalizes_trusted_context_and_plan_hash() -> None:
    server, client = _server()
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "delegate_task",
                "arguments": {
                    "prompt": "review the change",
                    "delegation_id": "delegation-1",
                    "input": {"ticket": "SDK-1"},
                },
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is False
    payload = client.created[0]
    assert payload["input"] == {
        "ticket": "SDK-1",
        "prompt": "review the change",
    }
    assert payload["policy_context"] == {
        "sandbox": "read_only",
        "network_policy": "deny",
    }
    assert payload["workspace_id"] == "workspace-1"
    assert payload["provider_id"] == "fake"
    assert len(payload["plan_hash"]) == 64


def test_delegate_task_rejects_gateway_owned_input() -> None:
    server, client = _server()
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "delegate_task",
                "arguments": {
                    "prompt": "unsafe",
                    "input": {"cwd": "D:/other"},
                },
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert client.created == []


def test_status_list_and_cancel_tools() -> None:
    server, client = _server()
    status = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "get_task_status",
                "arguments": {"delegation_id": "one"},
            },
        }
    )
    assert status is not None
    assert status["result"]["structuredContent"]["status"] == "active"

    listed = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "list_tasks",
                "arguments": {"status": "active"},
            },
        }
    )
    assert listed is not None
    assert listed["result"]["structuredContent"]["count"] == 1

    cancelled = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "cancel_task",
                "arguments": {"delegation_id": "one"},
            },
        }
    )
    assert cancelled is not None
    assert client.cancelled[0][0] == "one"
    assert client.cancelled[0][1]["reason"] == "cancelled through MCP"


def test_stdio_ignores_notifications_and_emits_one_line_per_response() -> None:
    server, _ = _server()
    input_stream = io.StringIO(
        "\n".join(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": "ping-1", "method": "ping"}),
            )
        )
        + "\n"
    )
    output_stream = io.StringIO()

    server.run(input_stream, output_stream)

    lines = output_stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "jsonrpc": "2.0",
        "id": "ping-1",
        "result": {},
    }
