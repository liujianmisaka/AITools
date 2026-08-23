from __future__ import annotations

import io
import json
from collections.abc import Mapping
from typing import Any

import pytest

from misaka_mcp_gateway.config import GatewayConfig
from misaka_mcp_gateway.server import McpStdioServer


class FakeClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[tuple[str, dict[str, Any]]] = []
        self.status_calls: list[tuple[str, float | None]] = []
        self.status_sequences: dict[str, list[dict[str, Any]]] = {}
        self.model_catalogs: list[dict[str, Any]] = [
            {
                "provider_id": "fake",
                "models": [
                    {
                        "model_id": "fake/model",
                        "display_name": "Fake model",
                        "description": "Deterministic test model",
                        "supported_efforts": ["low", "medium", "high"],
                    }
                ],
            }
        ]

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

    def list_model_catalogs(self) -> list[dict[str, Any]]:
        return self.model_catalogs

    def get_delegation(
        self,
        delegation_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.status_calls.append((delegation_id, timeout_seconds))
        sequence = self.status_sequences.get(delegation_id)
        if sequence:
            snapshot = sequence.pop(0)
            if not sequence:
                self.status_sequences[delegation_id] = [snapshot]
            return dict(snapshot)
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


def _server(
    config: GatewayConfig | None = None,
) -> tuple[McpStdioServer, FakeClient]:
    client = FakeClient()
    return (
        McpStdioServer(
            config
            or GatewayConfig(
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
        "wait_task",
        "list_execution_options",
        "get_task_status",
        "list_tasks",
        "cancel_task",
    }
    delegate_tool = next(
        tool for tool in listed["result"]["tools"] if tool["name"] == "delegate_task"
    )
    assert delegate_tool["inputSchema"]["required"] == ["prompt", "cwd"]
    assert {"provider_id", "model", "effort"}.issubset(delegate_tool["inputSchema"]["properties"])


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
                    "cwd": "D:/dev/project-one",
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
    assert payload["cwd"] == "D:/dev/project-one"
    assert payload["provider_id"] == "fake"
    assert payload["model"] == "fake/model"
    assert payload["effort"] == "high"
    assert payload["channel_id"] == "delegation-channel:delegation-1"
    assert len(payload["plan_hash"]) == 64


def test_delegate_task_returns_immediately_by_default() -> None:
    server, client = _server()

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "trigger-only",
            "method": "tools/call",
            "params": {
                "name": "delegate_task",
                "arguments": {
                    "prompt": "run a long task",
                    "cwd": "D:/dev/project-one",
                    "delegation_id": "trigger-only",
                },
            },
        }
    )

    assert response is not None
    result = response["result"]["structuredContent"]
    assert result == {
        "delegation_id": "trigger-only",
        "status": "proposed",
        "timed_out": False,
        "waited_ms": 0,
        "terminal": False,
        "next_action": "wait_task",
    }
    assert client.status_calls == []


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _patch_wait_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr("misaka_mcp_gateway.server.time.monotonic", clock.monotonic)
    monkeypatch.setattr("misaka_mcp_gateway.server.time.sleep", clock.sleep)
    monkeypatch.setattr("misaka_mcp_gateway.server._WAIT_POLL_INTERVAL_SECONDS", 0.01)
    return clock


def test_delegate_task_can_wait_until_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _patch_wait_clock(monkeypatch)
    server, client = _server()
    client.status_sequences["wait-then-complete"] = [
        {"delegation_id": "wait-then-complete", "status": "active"},
        {
            "delegation_id": "wait-then-complete",
            "status": "completed",
            "report": {"output": {"answer": "done"}},
        },
    ]

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "bounded-complete",
            "method": "tools/call",
            "params": {
                "name": "delegate_task",
                "arguments": {
                    "prompt": "finish quickly",
                    "cwd": "D:/dev/project-one",
                    "delegation_id": "wait-then-complete",
                    "wait_timeout_ms": 100,
                },
            },
        }
    )

    assert response is not None
    result = response["result"]["structuredContent"]
    assert result["status"] == "completed"
    assert result["terminal"] is True
    assert result["timed_out"] is False
    assert result["waited_ms"] == 10
    assert clock.value == 0.01
    assert [call[0] for call in client.status_calls] == [
        "wait-then-complete",
        "wait-then-complete",
    ]
    assert client.status_calls[0][1] == pytest.approx(0.1)
    assert client.status_calls[1][1] == pytest.approx(0.09)


def test_delegate_task_timeout_returns_active_without_cancelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_wait_clock(monkeypatch)
    server, client = _server()
    client.status_sequences["still-running"] = [
        {"delegation_id": "still-running", "status": "active"},
    ]

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "bounded-timeout",
            "method": "tools/call",
            "params": {
                "name": "delegate_task",
                "arguments": {
                    "prompt": "run slowly",
                    "cwd": "D:/dev/project-one",
                    "delegation_id": "still-running",
                    "wait_timeout_ms": 25,
                },
            },
        }
    )

    assert response is not None
    result = response["result"]["structuredContent"]
    assert result["status"] == "active"
    assert result["timed_out"] is True
    assert result["terminal"] is False
    assert result["waited_ms"] == 25
    assert result["next_action"] == "wait_task"
    assert client.cancelled == []


@pytest.mark.parametrize("timeout_value", (-1, 300_001, 1.5, True, "10", None))
def test_delegate_task_rejects_invalid_wait_timeout(
    timeout_value: object,
) -> None:
    server, client = _server()
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "invalid-wait-timeout",
            "method": "tools/call",
            "params": {
                "name": "delegate_task",
                "arguments": {
                    "prompt": "do not create",
                    "cwd": "D:/dev/project-one",
                    "wait_timeout_ms": timeout_value,
                },
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert "wait_timeout_ms must be an integer" in response["result"]["content"][0]["text"]
    assert client.created == []


def test_wait_task_returns_terminal_result_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_wait_clock(monkeypatch)
    server, client = _server()
    client.status_sequences["already-done"] = [
        {
            "delegation_id": "already-done",
            "status": "completed",
            "report": {"output": {"answer": "done"}},
        },
    ]

    def call_wait(request_id: str, compact: bool = False) -> dict[str, Any]:
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": "wait_task",
                    "arguments": {
                        "delegation_id": "already-done",
                        "timeout_ms": 100,
                        "compact": compact,
                    },
                },
            }
        )
        assert response is not None
        return response["result"]["structuredContent"]

    full = call_wait("wait-full")
    compact = call_wait("wait-compact", compact=True)

    assert full["status"] == compact["status"] == "completed"
    assert full["report"]["output"] == {"answer": "done"}
    assert "output" not in compact["report"]
    assert full["terminal"] is compact["terminal"] is True
    assert len(client.status_calls) == 2


def test_wait_task_timeout_does_not_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_wait_clock(monkeypatch)
    server, client = _server()
    client.status_sequences["wait-later"] = [
        {"delegation_id": "wait-later", "status": "active"},
    ]

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "wait-later-call",
            "method": "tools/call",
            "params": {
                "name": "wait_task",
                "arguments": {"delegation_id": "wait-later", "timeout_ms": 20},
            },
        }
    )

    assert response is not None
    result = response["result"]["structuredContent"]
    assert result["timed_out"] is True
    assert result["status"] == "active"
    assert client.cancelled == []


def test_delegate_task_resolves_call_execution_options_before_defaults() -> None:
    server, client = _server()
    common_arguments = {
        "prompt": "review the change",
        "cwd": "D:/dev/project-one",
        "delegation_id": "delegation-route-sensitive",
    }

    for request_id, execution_options in enumerate(
        (
            {},
            {
                "provider_id": "pixel",
                "model": "pixel/coder",
                "effort": "medium",
            },
        ),
        start=1,
    ):
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": "delegate_task",
                    "arguments": {**common_arguments, **execution_options},
                },
            }
        )
        assert response is not None
        assert response["result"]["isError"] is False

    assert [
        (payload["provider_id"], payload["model"], payload["effort"]) for payload in client.created
    ] == [
        ("fake", "fake/model", "high"),
        ("pixel", "pixel/coder", "medium"),
    ]
    assert client.created[0]["input"] == client.created[1]["input"]
    assert client.created[0]["plan_hash"] != client.created[1]["plan_hash"]


def test_delegate_task_accepts_call_options_without_gateway_defaults() -> None:
    server, client = _server(GatewayConfig())

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "call-options-only",
            "method": "tools/call",
            "params": {
                "name": "delegate_task",
                "arguments": {
                    "prompt": "review the change",
                    "cwd": "D:/dev/project-one",
                    "provider_id": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                },
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is False
    assert client.created[0]["provider_id"] == "codex"


def test_delegate_task_rejects_missing_execution_option_without_default() -> None:
    server, client = _server(GatewayConfig())

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "missing-effort",
            "method": "tools/call",
            "params": {
                "name": "delegate_task",
                "arguments": {
                    "prompt": "review the change",
                    "cwd": "D:/dev/project-one",
                    "provider_id": "codex",
                    "model": "gpt-5.6-sol",
                },
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == (
        "delegate_task requires effort as an argument or gateway default"
    )
    assert client.created == []


def test_delegate_task_preserves_a_different_cwd_for_each_call() -> None:
    server, client = _server()

    for index, cwd in enumerate(("D:/dev/project-one", "E:/sources/project-two"), start=1):
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {
                    "name": "delegate_task",
                    "arguments": {
                        "prompt": "review the change",
                        "cwd": cwd,
                        "delegation_id": "delegation-path-sensitive",
                    },
                },
            }
        )
        assert response is not None
        assert response["result"]["isError"] is False

    assert [payload["cwd"] for payload in client.created] == [
        "D:/dev/project-one",
        "E:/sources/project-two",
    ]
    assert client.created[0]["plan_hash"] != client.created[1]["plan_hash"]


def test_delegate_task_requires_cwd() -> None:
    server, client = _server()

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "missing-cwd",
            "method": "tools/call",
            "params": {
                "name": "delegate_task",
                "arguments": {"prompt": "review the change"},
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "cwd must be a non-empty string"
    assert client.created == []


@pytest.mark.parametrize(
    "reserved_field",
    ("cwd", "sandbox", "provider_id", "model", "effort"),
)
def test_delegate_task_rejects_gateway_owned_input(reserved_field: str) -> None:
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
                    "cwd": "D:/dev/project-one",
                    "input": {reserved_field: "untrusted"},
                },
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert reserved_field in response["result"]["content"][0]["text"]
    assert client.created == []


def test_list_execution_options_proxies_control_plane_catalogs() -> None:
    server, client = _server()

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "execution-options",
            "method": "tools/call",
            "params": {
                "name": "list_execution_options",
                "arguments": {},
            },
        }
    )

    assert response is not None
    assert response["result"]["structuredContent"] == {
        "providers": client.model_catalogs,
        "count": 1,
    }


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
