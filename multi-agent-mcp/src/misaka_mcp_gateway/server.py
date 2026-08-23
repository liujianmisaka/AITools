from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Protocol, TextIO, cast

from misaka_mcp_gateway.client import ControlPlaneError
from misaka_mcp_gateway.config import GatewayConfig

_MODERN_PROTOCOL_VERSION = "2026-07-28"
_LATEST_LEGACY_PROTOCOL_VERSION = "2025-11-25"
_SUPPORTED_LEGACY_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    _LATEST_LEGACY_PROTOCOL_VERSION,
}
_SUPPORTED_PROTOCOL_VERSIONS = (
    _MODERN_PROTOCOL_VERSION,
    _LATEST_LEGACY_PROTOCOL_VERSION,
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
_PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
_PLAN_HASH = re.compile(r"^[0-9a-f]{64}$")


class ControlPlanePort(Protocol):
    def create_delegation(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def list_model_catalogs(self) -> list[dict[str, Any]]: ...

    def get_delegation(self, delegation_id: str) -> dict[str, Any]: ...

    def list_delegations(self) -> list[dict[str, Any]]: ...

    def cancel_delegation(
        self,
        delegation_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]: ...


class McpStdioServer:
    """Minimal MCP server that maps tools to the Control Plane HTTP API."""

    def __init__(self, config: GatewayConfig, client: ControlPlanePort) -> None:
        self._config = config
        self._client = client

    def run(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        input_stream = stdin or sys.stdin
        output_stream = stdout or sys.stdout
        for line in input_stream:
            if not line.strip():
                continue
            try:
                message: object = json.loads(line)
            except json.JSONDecodeError:
                response = _error(None, -32700, "Parse error")
            else:
                response = self.handle_message(message)
            if response is not None:
                output_stream.write(
                    json.dumps(
                        response,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                output_stream.flush()

    def handle_message(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, Mapping):
            return _error(None, -32600, "Invalid Request")
        request = cast(Mapping[str, Any], message)
        method = request.get("method")
        if not isinstance(method, str):
            return _notification_or_error(request, -32600, "Invalid Request")
        if method == "notifications/initialized":
            return None
        if "id" not in request:
            return None
        request_id: object = request["id"]
        modern_version = _request_protocol_version(request)
        if method == "server/discover":
            if modern_version is not None and modern_version != _MODERN_PROTOCOL_VERSION:
                return _unsupported_protocol_error(request_id, modern_version)
            return _result(request_id, _discovery_result(), modern=True)
        if modern_version is not None and modern_version != _MODERN_PROTOCOL_VERSION:
            return _unsupported_protocol_error(request_id, modern_version)
        modern = modern_version == _MODERN_PROTOCOL_VERSION
        if method == "initialize":
            params = request.get("params")
            requested_version = (
                cast(Mapping[str, Any], params).get("protocolVersion")
                if isinstance(params, Mapping)
                else None
            )
            return _result(
                request_id,
                {
                    "protocolVersion": _select_protocol_version(requested_version),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "misaka-multi-agent-mcp",
                        "version": "0.1.0",
                    },
                    "instructions": (
                        "Use list_execution_options to discover providers, models, and "
                        "supported efforts. Use delegate_task with an explicit cwd and "
                        "execution selection to create work in the configured V3 Control "
                        "Plane. Call-level provider_id, model, and effort override gateway "
                        "defaults. The gateway still enforces its configured actor, sandbox, "
                        "and network policy."
                    ),
                },
            )
        if method == "ping":
            return _result(request_id, {}, modern=modern)
        if method == "tools/list":
            result: dict[str, Any] = {"tools": list(_tool_definitions())}
            if modern:
                result.update(
                    {
                        "resultType": "complete",
                        "ttlMs": 300_000,
                        "cacheScope": "public",
                    }
                )
            return _result(request_id, result, modern=modern)
        if method == "tools/call":
            params = request.get("params")
            if not isinstance(params, Mapping):
                return _error(
                    request_id,
                    -32602,
                    "tools/call params must be an object",
                )
            return _result(
                request_id,
                self._call_tool(
                    cast(Mapping[str, Any], params),
                    modern=modern,
                ),
                modern=modern,
            )
        return _error(request_id, -32601, f"Method not found: {method}")

    def _call_tool(
        self,
        params: Mapping[str, Any],
        *,
        modern: bool,
    ) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return _tool_error(
                "tools/call requires a tool name and object arguments",
                modern=modern,
            )
        normalized = {
            str(key): value for key, value in cast(Mapping[object, Any], arguments).items()
        }
        try:
            if name == "delegate_task":
                value = self._delegate_task(normalized)
            elif name == "list_execution_options":
                value = self._list_execution_options(normalized)
            elif name == "get_task_status":
                value = self._get_task_status(normalized)
            elif name == "list_tasks":
                value = self._list_tasks(normalized)
            elif name == "cancel_task":
                value = self._cancel_task(normalized)
            else:
                return _tool_error(f"Unknown tool: {name}", modern=modern)
            return _tool_result(value, modern=modern)
        except (ControlPlaneError, TypeError, ValueError) as exc:
            return _tool_error(str(exc), modern=modern)

    def _delegate_task(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _ensure_only(
            arguments,
            {
                "prompt",
                "cwd",
                "delegation_id",
                "idempotency_key",
                "input",
                "mode",
                "session_id",
                "channel_id",
                "parent_delegation_id",
                "output_schema",
                "plan_hash",
                "decision_ref",
                "required_features",
                "observers",
                "policy",
                "provider_id",
                "model",
                "effort",
            },
        )
        prompt = _required_argument(arguments, "prompt")
        cwd = _required_argument(arguments, "cwd")
        provider_id = _execution_option(
            arguments,
            "provider_id",
            self._config.provider_id,
        )
        model = _execution_option(arguments, "model", self._config.model)
        effort = _execution_option(arguments, "effort", self._config.effort)
        extra_input = _object_argument(arguments, "input", default={})
        reserved_input_fields = {
            "cwd",
            "sandbox",
            "provider_id",
            "model",
            "effort",
        }
        conflicting_fields = sorted(reserved_input_fields.intersection(extra_input))
        if conflicting_fields:
            fields = ", ".join(conflicting_fields)
            raise ValueError(
                f"delegate_task.input cannot contain execution context fields: {fields}"
            )
        delegation_id = _optional_string(arguments, "delegation_id") or _new_id("delegation")
        idempotency_key = _optional_string(arguments, "idempotency_key") or delegation_id
        mode = arguments.get("mode", "one_shot")
        if mode not in {"one_shot", "continuable"}:
            raise ValueError("delegate_task.mode must be one_shot or continuable")
        channel_id = _optional_string(arguments, "channel_id") or (
            f"delegation-channel:{delegation_id}"
        )
        request_input = dict(extra_input)
        request_input["prompt"] = prompt
        payload: dict[str, Any] = {
            "actor": self._config.actor,
            "delegation_id": delegation_id,
            "idempotency_key": idempotency_key,
            "initiator": self._config.actor,
            "controller": self._config.actor,
            "scope": self._config.scope,
            "capability_id": self._config.capability_id,
            "operation": self._config.operation,
            "input": request_input,
            "cwd": cwd,
            "provider_id": provider_id,
            "model": model,
            "effort": effort,
            "policy_context": {
                "sandbox": self._config.sandbox,
                "network_policy": self._config.network_policy,
            },
            "output_schema": _nullable_object_argument(
                arguments,
                "output_schema",
            ),
            "plan_hash": "0" * 64,
            "mode": mode,
            "parent_delegation_id": _optional_string(
                arguments,
                "parent_delegation_id",
            ),
            "session_id": _optional_string(arguments, "session_id"),
            "channel_id": channel_id,
            "decision_ref": _nullable_object_argument(
                arguments,
                "decision_ref",
            ),
            "required_features": _string_list_argument(
                arguments,
                "required_features",
            ),
            "observers": _object_list_argument(arguments, "observers"),
            "policy": _object_argument(arguments, "policy", default={}),
        }
        plan_hash = _optional_string(arguments, "plan_hash")
        if plan_hash is not None and _PLAN_HASH.fullmatch(plan_hash) is None:
            raise ValueError("delegate_task.plan_hash must contain 64 lowercase hex digits")
        payload["plan_hash"] = plan_hash or _plan_hash(payload)
        return self._client.create_delegation(payload)

    def _list_execution_options(
        self,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        _ensure_only(arguments, set())
        providers = self._client.list_model_catalogs()
        return {"providers": providers, "count": len(providers)}

    def _get_task_status(
        self,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        _ensure_only(arguments, {"delegation_id"})
        delegation_id = _required_argument(arguments, "delegation_id")
        return self._client.get_delegation(delegation_id)

    def _list_tasks(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _ensure_only(arguments, {"status", "limit"})
        status = _optional_string(arguments, "status")
        limit = arguments.get("limit", 50)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("list_tasks.limit must be an integer between 1 and 100")
        tasks = self._client.list_delegations()
        if status:
            tasks = [task for task in tasks if task.get("status") == status]
        selected = tasks[:limit]
        return {"tasks": selected, "count": len(selected)}

    def _cancel_task(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _ensure_only(
            arguments,
            {
                "delegation_id",
                "request_id",
                "idempotency_key",
                "reason",
                "session_id",
                "expected_activation_id",
            },
        )
        delegation_id = _required_argument(arguments, "delegation_id")
        request_id = _optional_string(arguments, "request_id") or _new_id("cancel")
        payload = {
            "request_id": request_id,
            "idempotency_key": (_optional_string(arguments, "idempotency_key") or request_id),
            "actor": self._config.actor,
            "session_id": _optional_string(arguments, "session_id"),
            "expected_activation_id": _optional_string(
                arguments,
                "expected_activation_id",
            ),
            "reason": (_optional_string(arguments, "reason") or "cancelled through MCP"),
        }
        return self._client.cancel_delegation(delegation_id, payload)


def _select_protocol_version(value: object) -> str:
    if isinstance(value, str) and value in _SUPPORTED_LEGACY_PROTOCOL_VERSIONS:
        return value
    return _LATEST_LEGACY_PROTOCOL_VERSION


def _request_protocol_version(request: Mapping[str, Any]) -> str | None:
    params = request.get("params")
    if not isinstance(params, Mapping):
        return None
    metadata = cast(Mapping[str, Any], params).get("_meta")
    if not isinstance(metadata, Mapping):
        return None
    version = cast(Mapping[str, Any], metadata).get(_PROTOCOL_VERSION_META_KEY)
    return version if isinstance(version, str) else None


def _discovery_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": list(_SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {"listChanged": False}},
        "_meta": {
            "io.modelcontextprotocol/serverInfo": {
                "name": "misaka-multi-agent-mcp",
                "version": "0.1.0",
            }
        },
        "instructions": (
            "Delegate and observe tasks through the configured Multi-Agent V3 "
            "Control Plane. Each delegation must supply cwd explicitly; nested input "
            "cannot override gateway-owned execution context. Discover valid provider, "
            "model, and effort combinations with list_execution_options."
        ),
        "ttlMs": 3_600_000,
        "cacheScope": "public",
    }


def _tool_definitions() -> Iterable[dict[str, Any]]:
    yield {
        "name": "delegate_task",
        "description": ("Delegate one task to the configured Multi-Agent V3 Control Plane."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Task instructions.",
                },
                "cwd": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Absolute working directory for this delegation.",
                },
                "provider_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Provider registered in the Control Plane; overrides the gateway default."
                    ),
                },
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Model exposed by the selected provider; overrides the gateway default."
                    ),
                },
                "effort": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Supported effort for the selected model; overrides the gateway default."
                    ),
                },
                "delegation_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "input": {
                    "type": "object",
                    "additionalProperties": True,
                },
                "mode": {
                    "type": "string",
                    "enum": ["one_shot", "continuable"],
                },
                "session_id": {"type": "string"},
                "channel_id": {"type": "string"},
                "parent_delegation_id": {"type": "string"},
                "output_schema": {"type": "object"},
                "plan_hash": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "decision_ref": {"type": "object"},
                "required_features": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "observers": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "policy": {"type": "object"},
            },
            "required": ["prompt", "cwd"],
            "additionalProperties": False,
        },
    }
    yield {
        "name": "list_execution_options",
        "description": (
            "List providers, models, and supported efforts exposed by the Control Plane."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
    yield {
        "name": "get_task_status",
        "description": "Read the configured actor's delegation status.",
        "inputSchema": {
            "type": "object",
            "properties": {"delegation_id": {"type": "string"}},
            "required": ["delegation_id"],
            "additionalProperties": False,
        },
    }
    yield {
        "name": "list_tasks",
        "description": "List delegations visible to the configured actor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "additionalProperties": False,
        },
    }
    yield {
        "name": "cancel_task",
        "description": "Request cancellation of a delegation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "delegation_id": {"type": "string"},
                "request_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "reason": {"type": "string"},
                "session_id": {"type": "string"},
                "expected_activation_id": {"type": "string"},
            },
            "required": ["delegation_id"],
            "additionalProperties": False,
        },
    }


def _result(
    request_id: object,
    result: Mapping[str, Any],
    *,
    modern: bool = False,
) -> dict[str, Any]:
    result_value = dict(result)
    if modern:
        metadata = result_value.setdefault("_meta", {})
        if isinstance(metadata, Mapping):
            result_value["_meta"] = {
                **cast(Mapping[str, Any], metadata),
                "io.modelcontextprotocol/serverInfo": {
                    "name": "misaka-multi-agent-mcp",
                    "version": "0.1.0",
                },
            }
    return {"jsonrpc": "2.0", "id": request_id, "result": result_value}


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _unsupported_protocol_error(
    request_id: object,
    requested: str,
) -> dict[str, Any]:
    response = _error(request_id, -32022, "Unsupported protocol version")
    response["error"]["data"] = {
        "supported": list(_SUPPORTED_PROTOCOL_VERSIONS),
        "requested": requested,
    }
    return response


def _notification_or_error(
    message: Mapping[str, Any],
    code: int,
    detail: str,
) -> dict[str, Any] | None:
    if "id" not in message:
        return None
    return _error(message["id"], code, detail)


def _tool_result(value: dict[str, Any], *, modern: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False),
            }
        ],
        "structuredContent": value,
        "isError": False,
    }
    if modern:
        result["resultType"] = "complete"
    return result


def _tool_error(message: str, *, modern: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }
    if modern:
        result["resultType"] = "complete"
    return result


def _ensure_only(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ValueError(f"unsupported tool arguments: {fields}")


def _execution_option(
    arguments: Mapping[str, Any],
    name: str,
    default: str | None,
) -> str:
    value = _optional_string(arguments, name)
    if value is not None:
        return value
    if default is None:
        raise ValueError(f"delegate_task requires {name} as an argument or gateway default")
    return default


def _required_argument(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(
    arguments: Mapping[str, Any],
    name: str,
) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string when provided")
    return value


def _object_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    default: Mapping[str, Any],
) -> dict[str, Any]:
    value = arguments.get(name, default)
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in cast(Mapping[object, Any], value).items()}


def _nullable_object_argument(
    arguments: Mapping[str, Any],
    name: str,
) -> dict[str, Any] | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object when provided")
    return {str(key): item for key, item in cast(Mapping[object, Any], value).items()}


def _string_list_argument(
    arguments: Mapping[str, Any],
    name: str,
) -> list[str]:
    value = arguments.get(name, [])
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array of non-empty strings")
    result: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} must be an array of non-empty strings")
        result.append(item)
    return result


def _object_list_argument(
    arguments: Mapping[str, Any],
    name: str,
) -> list[dict[str, Any]]:
    value = arguments.get(name, [])
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array of objects")
    result: list[dict[str, Any]] = []
    for item in cast(list[object], value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{name} must be an array of objects")
        result.append(
            {
                str(key): item_value
                for key, item_value in cast(
                    Mapping[object, Any],
                    item,
                ).items()
            }
        )
    return result


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _plan_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
