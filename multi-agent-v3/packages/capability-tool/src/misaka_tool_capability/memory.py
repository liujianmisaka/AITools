from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Mapping

from misaka_invocation_contracts import CapabilityDescriptor, CapabilityFeature
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    JsonObject,
    JsonValue,
    ModuleId,
    ModuleManifest,
    ServiceProvision,
)

from misaka_tool_capability.contracts import (
    TOOL_PROVIDER_SERVICE,
    ToolDescriptor,
    ToolHandler,
    ToolInvocation,
    ToolResult,
    ToolStatus,
    tool_descriptor,
)

MEMORY_TOOL_MODULE_ID = ModuleId("capability.tool.memory")


class MemoryToolProvider:
    """Deterministic in-process provider for contract tests and local profiles."""

    def __init__(
        self,
        tools: Mapping[str, tuple[ToolDescriptor, ToolHandler]] | None = None,
    ) -> None:
        self._tools: dict[str, tuple[ToolDescriptor, ToolHandler]] = {}
        self._results: dict[str, tuple[str, ToolResult]] = {}
        self._pending: dict[str, tuple[str, asyncio.Task[ToolResult]]] = {}
        self._tasks: dict[str, asyncio.Task[ToolResult]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        for tool_id, registration in (tools or {}).items():
            self.register(tool_id, *registration)

    def register(self, tool_id: str, descriptor: ToolDescriptor, handler: ToolHandler) -> None:
        if self._closed:
            raise RuntimeError("tool provider is closed")
        if tool_id != descriptor.tool_id:
            raise ValueError("registered tool id must match descriptor.tool_id")
        if tool_id in self._tools:
            raise ValueError(f"tool {tool_id} is already registered")
        self._tools[tool_id] = (descriptor, handler)

    async def describe(self) -> CapabilityDescriptor:
        return tool_descriptor(
            features=frozenset(
                {CapabilityFeature.STRUCTURED_OUTPUT, CapabilityFeature.CANCELLATION}
            )
        )

    async def tools(self) -> tuple[ToolDescriptor, ...]:
        async with self._lock:
            return tuple(descriptor for descriptor, _ in self._tools.values())

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        async with self._lock:
            if self._closed:
                return _result(
                    invocation,
                    ToolStatus.REJECTED,
                    "tool.provider_closed",
                    "tool provider is closed",
                )
            registration = self._tools.get(invocation.tool_id)
            if registration is None:
                return _result(
                    invocation,
                    ToolStatus.REJECTED,
                    "tool.not_found",
                    "tool was not found",
                )
            descriptor, handler = registration
            fingerprint = _fingerprint(invocation.tool_id, invocation.arguments)
            existing = self._results.get(invocation.idempotency_key)
            if existing is not None:
                existing_fingerprint, result = existing
                if existing_fingerprint != fingerprint:
                    return _result(
                        invocation,
                        ToolStatus.REJECTED,
                        "tool.idempotency_conflict",
                        "idempotency key was used with different tool arguments",
                    )
                return result
            if not _matches_json_schema(invocation.arguments, descriptor.input_schema):
                result = _result(
                    invocation,
                    ToolStatus.REJECTED,
                    "tool.input_contract_violated",
                    "tool arguments do not satisfy input_schema",
                )
                self._results[invocation.idempotency_key] = (fingerprint, result)
                return result

            pending = self._pending.get(invocation.idempotency_key)
            if pending is not None:
                pending_fingerprint, task = pending
                if pending_fingerprint != fingerprint:
                    return _result(
                        invocation,
                        ToolStatus.REJECTED,
                        "tool.idempotency_conflict",
                        "idempotency key was used with different tool arguments",
                    )
            else:
                task = asyncio.create_task(self._run_handler(invocation, descriptor, handler))
                self._pending[invocation.idempotency_key] = (fingerprint, task)
            self._tasks[invocation.invocation_id] = task
        result: ToolResult | None = None
        try:
            result = await task
        except asyncio.CancelledError:
            result = _result(
                invocation,
                ToolStatus.CANCELLED,
                "tool.cancelled",
                "tool invocation was cancelled",
            )
        finally:
            if result is None:
                result = _result(
                    invocation,
                    ToolStatus.FAILED,
                    "tool.execution_failed",
                    "tool handler terminated without a result",
                )
            async with self._lock:
                self._tasks.pop(invocation.invocation_id, None)
                current = self._pending.get(invocation.idempotency_key)
                if current is not None and current[1] is task:
                    self._pending.pop(invocation.idempotency_key, None)
                    self._results.setdefault(invocation.idempotency_key, (fingerprint, result))
        return result

    async def cancel(self, invocation_id: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        async with self._lock:
            task = self._tasks.get(invocation_id)
            if task is not None:
                task.cancel()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = tuple(self._tasks.values())
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_handler(
        self,
        invocation: ToolInvocation,
        descriptor: ToolDescriptor,
        handler: ToolHandler,
    ) -> ToolResult:
        try:
            value = handler(invocation.arguments)
            output = await value if inspect.isawaitable(value) else value
            try:
                json.dumps(output, ensure_ascii=False)
            except (TypeError, ValueError):
                return _result(
                    invocation,
                    ToolStatus.FAILED,
                    "tool.output_not_json",
                    "tool handler returned a non-JSON value",
                )
            if not _matches_json_schema(output, descriptor.output_schema):
                return _result(
                    invocation,
                    ToolStatus.FAILED,
                    "tool.output_contract_violated",
                    "tool output does not satisfy output_schema",
                )
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_id=invocation.tool_id,
                status=ToolStatus.SUCCEEDED,
                output=output,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _result(
                invocation,
                ToolStatus.FAILED,
                "tool.execution_failed",
                str(exc) or exc.__class__.__name__,
            )


class MemoryToolModule:
    def __init__(self, provider: MemoryToolProvider | None = None) -> None:
        self.provider = provider or MemoryToolProvider()

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=MEMORY_TOOL_MODULE_ID,
            version="1.0.0",
            provides=(ServiceProvision(TOOL_PROVIDER_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        context.provide(TOOL_PROVIDER_SERVICE, self.provider, version="1.0.0")

        async def dispose() -> None:
            await self.provider.close()

        return dispose

    async def start(self, context: HostContext) -> None:
        del context


def _result(
    invocation: ToolInvocation,
    status: ToolStatus,
    error_code: str,
    error_message: str,
) -> ToolResult:
    return ToolResult(
        invocation_id=invocation.invocation_id,
        tool_id=invocation.tool_id,
        status=status,
        error_code=error_code,
        error_message=error_message,
    )


def _fingerprint(tool_id: str, arguments: JsonObject) -> str:
    canonical = json.dumps(
        {"tool_id": tool_id, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _matches_json_schema(value: JsonValue, schema: JsonObject) -> bool:
    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str) or not _matches_type(value, schema_type)
    ):
        return False
    if isinstance(value, dict):
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            return False
        if not isinstance(properties, dict):
            return False
        if any(item not in value for item in required):
            return False
        if schema.get("additionalProperties", True) is False and set(value) - set(properties):
            return False
        for key, item in value.items():
            property_schema = properties.get(key)
            if property_schema is not None and (
                not isinstance(property_schema, dict)
                or not _matches_json_schema(item, property_schema)
            ):
                return False
    elif isinstance(value, list) and schema.get("items") is not None:
        items = schema["items"]
        if not isinstance(items, dict):
            return False
        if not all(_matches_json_schema(item, items) for item in value):
            return False
    return True


def _matches_type(value: JsonValue, expected_type: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "null": value is None,
    }.get(expected_type, False)
