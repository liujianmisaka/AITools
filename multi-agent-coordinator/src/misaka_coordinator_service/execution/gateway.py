from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from agent_framework import Content

from misaka_coordinator_service.domain._serialization import ensure_text
from misaka_coordinator_service.execution.contracts import (
    DelegationCancelRequest,
    DelegationMessageRequest,
    DelegationReconciliationRequest,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
    ExecutionProviderCatalog,
    JsonObject,
    JsonValue,
    MessageDispatchSnapshot,
)
from misaka_coordinator_service.tools import (
    ToolCallResult,
    ToolNotAvailableError,
    ToolRegistryError,
)

V3_DEFAULT_ALLOWED_TOOLS = (
    "delegate_task",
    "send_task_message",
    "wait_task",
    "list_execution_options",
    "get_task_status",
    "list_tasks",
    "cancel_task",
    "resolve_task_reconciliation",
)

V3_DEFAULT_CAPABILITIES_BY_TOOL: Mapping[str, Sequence[str]] = {
    "delegate_task": ("delegation.create",),
    "send_task_message": ("delegation.message",),
    "wait_task": ("delegation.observe",),
    "list_execution_options": ("execution.discovery",),
    "get_task_status": ("delegation.observe",),
    "list_tasks": ("delegation.observe",),
    "cancel_task": ("delegation.cancel",),
    "resolve_task_reconciliation": ("delegation.reconcile",),
}


class V3ExecutionGatewayError(RuntimeError):
    """Base error for the V3 public execution adapter."""


class V3ToolUnavailableError(V3ExecutionGatewayError):
    """Raised when the configured V3 MCP tool is unavailable."""


class V3ToolInvocationError(V3ExecutionGatewayError):
    """Raised when the V3 MCP tool call fails."""


class V3ProtocolError(V3ExecutionGatewayError):
    """Raised when a V3 MCP response violates the adapter contract."""


class ToolCaller(Protocol):
    async def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> ToolCallResult: ...


@dataclass(frozen=True, slots=True)
class V3ToolNames:
    delegate: str = "delegate_task"
    send_message: str = "send_task_message"
    wait: str = "wait_task"
    list_execution_options: str = "list_execution_options"
    get_status: str = "get_task_status"
    list_tasks: str = "list_tasks"
    cancel: str = "cancel_task"
    resolve_reconciliation: str = "resolve_task_reconciliation"

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                field_name,
                ensure_text(getattr(self, field_name), field_name),
            )


class V3ExecutionGateway:
    def __init__(
        self,
        *,
        tools: ToolCaller,
        tool_names: V3ToolNames | None = None,
    ) -> None:
        self._tools = tools
        self._tool_names = tool_names or V3ToolNames()

    async def delegate(self, request: DelegationRequest) -> DelegationSnapshot:
        arguments: dict[str, object] = {
            "prompt": request.prompt,
            "cwd": request.cwd,
            "provider_id": request.selection.provider_id,
            "model": request.selection.model,
            "effort": request.selection.effort,
            "mode": request.mode.value,
            "input": dict(request.input),
            "required_features": list(request.required_features),
            "observers": [dict(observer) for observer in request.observers],
            "policy": dict(request.policy),
            "wait_timeout_ms": 0,
        }
        _put_optional(arguments, "delegation_id", request.delegation_id)
        _put_optional(arguments, "idempotency_key", request.idempotency_key)
        _put_optional(arguments, "session_id", request.session_id)
        _put_optional(arguments, "channel_id", request.channel_id)
        _put_optional(arguments, "parent_delegation_id", request.parent_delegation_id)
        _put_optional_mapping(arguments, "output_schema", request.output_schema)
        _put_optional(arguments, "plan_hash", request.plan_hash)
        _put_optional_mapping(arguments, "decision_ref", request.decision_ref)
        return _parse_snapshot(
            await self._call_object(self._tool_names.delegate, arguments),
            self._tool_names.delegate,
        )

    async def get(self, delegation_id: str) -> DelegationSnapshot:
        return _parse_snapshot(
            await self._call_object(
                self._tool_names.get_status,
                {"delegation_id": ensure_text(delegation_id, "delegation_id")},
            ),
            self._tool_names.get_status,
        )

    async def wait(self, delegation_id: str, *, timeout_ms: int) -> DelegationSnapshot:
        if isinstance(timeout_ms, bool) or not 0 <= timeout_ms <= 300_000:
            raise V3ExecutionGatewayError("timeout_ms must be between 0 and 300000")
        return _parse_snapshot(
            await self._call_object(
                self._tool_names.wait,
                {
                    "delegation_id": ensure_text(delegation_id, "delegation_id"),
                    "timeout_ms": timeout_ms,
                    "compact": False,
                },
            ),
            self._tool_names.wait,
        )

    async def list(
        self,
        *,
        status: DelegationStatus | None = None,
        limit: int = 50,
    ) -> tuple[DelegationSnapshot, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise V3ExecutionGatewayError("limit must be between 1 and 100")
        arguments: dict[str, object] = {"limit": limit}
        if status is not None:
            arguments["status"] = status.value
        data = await self._call_object(self._tool_names.list_tasks, arguments)
        values = _object_list(data.get("tasks"), "tasks")
        return tuple(_parse_snapshot(value, self._tool_names.list_tasks) for value in values)

    async def send_message(self, request: DelegationMessageRequest) -> MessageDispatchSnapshot:
        arguments: dict[str, object] = {
            "delegation_id": request.delegation_id,
            "session_id": request.session_id,
            "message": request.message,
            "delivery": request.delivery.value,
        }
        _put_optional(arguments, "expected_activation_id", request.expected_activation_id)
        _put_optional(arguments, "dispatch_id", request.dispatch_id)
        _put_optional(arguments, "idempotency_key", request.idempotency_key)
        _put_optional(arguments, "message_id", request.message_id)
        _put_optional(arguments, "model", request.model)
        _put_optional(arguments, "effort", request.effort)
        return _parse_dispatch(
            await self._call_object(self._tool_names.send_message, arguments),
            self._tool_names.send_message,
        )

    async def cancel(self, request: DelegationCancelRequest) -> DelegationSnapshot:
        arguments: dict[str, object] = {
            "delegation_id": request.delegation_id,
            "reason": request.reason,
        }
        _put_optional(arguments, "request_id", request.request_id)
        _put_optional(arguments, "idempotency_key", request.idempotency_key)
        _put_optional(arguments, "session_id", request.session_id)
        _put_optional(arguments, "expected_activation_id", request.expected_activation_id)
        return _parse_snapshot(
            await self._call_object(self._tool_names.cancel, arguments),
            self._tool_names.cancel,
        )

    async def resolve_reconciliation(
        self,
        request: DelegationReconciliationRequest,
    ) -> DelegationSnapshot:
        arguments: dict[str, object] = {
            "delegation_id": request.delegation_id,
            "expected_revision": request.expected_revision,
            "status": request.status.value,
            "reason": request.reason,
            "output": request.output,
        }
        _put_optional(arguments, "request_id", request.request_id)
        _put_optional(arguments, "idempotency_key", request.idempotency_key)
        return _parse_snapshot(
            await self._call_object(self._tool_names.resolve_reconciliation, arguments),
            self._tool_names.resolve_reconciliation,
        )

    async def execution_options(self) -> tuple[ExecutionProviderCatalog, ...]:
        data = await self._call_object(self._tool_names.list_execution_options, {})
        providers = _object_list(data.get("providers"), "providers")
        return tuple(
            _parse_provider_catalog(provider, self._tool_names.list_execution_options)
            for provider in providers
        )

    async def _call_object(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> JsonObject:
        try:
            result = await self._tools.invoke(tool_name, arguments)
        except ToolNotAvailableError as error:
            raise V3ToolUnavailableError(f"V3 tool {tool_name} is unavailable") from error
        except ToolRegistryError as error:
            raise V3ToolInvocationError(f"V3 tool {tool_name} failed") from error
        return _tool_value_object(result.value, tool_name)


def _put_optional(target: dict[str, object], key: str, value: str | None) -> None:
    if value is not None:
        target[key] = value


def _put_optional_mapping(
    target: dict[str, object],
    key: str,
    value: Mapping[str, JsonValue] | None,
) -> None:
    if value is not None:
        target[key] = dict(value)


def _parse_snapshot(value: object, tool_name: str) -> DelegationSnapshot:
    try:
        return DelegationSnapshot.from_object(value)
    except ValueError as error:
        raise V3ProtocolError(
            f"V3 tool {tool_name} returned an invalid delegation: {error}"
        ) from error


def _parse_dispatch(value: object, tool_name: str) -> MessageDispatchSnapshot:
    try:
        return MessageDispatchSnapshot.from_object(value)
    except ValueError as error:
        raise V3ProtocolError(
            f"V3 tool {tool_name} returned an invalid message dispatch: {error}"
        ) from error


def _parse_provider_catalog(value: object, tool_name: str) -> ExecutionProviderCatalog:
    try:
        return ExecutionProviderCatalog.from_object(value)
    except ValueError as error:
        raise V3ProtocolError(
            f"V3 tool {tool_name} returned an invalid provider catalog: {error}"
        ) from error


def _tool_value_object(value: object, tool_name: str) -> JsonObject:
    candidate = value
    if isinstance(value, str):
        candidate = _load_json(value, tool_name)
    elif isinstance(value, list):
        contents = cast(list[object], value)
        if not contents or any(not isinstance(item, Content) for item in contents):
            raise V3ProtocolError(f"V3 tool {tool_name} returned unsupported content")
        text = "".join(item.text or "" for item in contents if isinstance(item, Content))
        candidate = _load_json(text, tool_name)
    if not isinstance(candidate, dict):
        raise V3ProtocolError(f"V3 tool {tool_name} must return a JSON object")
    raw = cast(dict[object, object], candidate)
    if any(not isinstance(key, str) for key in raw):
        raise V3ProtocolError(f"V3 tool {tool_name} returned non-string keys")
    return cast(JsonObject, raw)


def _load_json(value: str, tool_name: str) -> object:
    try:
        return cast(object, json.loads(value))
    except json.JSONDecodeError as error:
        raise V3ProtocolError(f"V3 tool {tool_name} returned invalid JSON") from error


def _object_list(value: object, field_name: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise V3ProtocolError(f"{field_name} must be a list")
    items: list[dict[str, object]] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, dict):
            raise V3ProtocolError(f"{field_name}[{index}] must be an object")
        raw = cast(dict[object, object], item)
        if any(not isinstance(key, str) for key in raw):
            raise V3ProtocolError(f"{field_name}[{index}] keys must be strings")
        items.append(cast(dict[str, object], raw))
    return tuple(items)
