from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping

from misaka_invocation_contracts import CapabilityDescriptor, CapabilityFeature
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import ModuleId, ModuleManifest, ServiceProvision

from misaka_tool_capability.contracts import (
    TOOL_PROVIDER_SERVICE,
    ToolDescriptor,
    ToolExecutionContext,
    ToolHandler,
    ToolInvocation,
    ToolResult,
    ToolStatus,
    tool_descriptor,
)

MEMORY_TOOL_MODULE_ID = ModuleId("capability.tool.memory")


class MemoryToolProvider:
    """Deterministic executor; admission and contracts belong to the pipeline."""

    def __init__(
        self,
        tools: Mapping[str, tuple[ToolDescriptor, ToolHandler]] | None = None,
    ) -> None:
        self._tools: dict[str, tuple[ToolDescriptor, ToolHandler]] = {}
        self._tasks: dict[str, asyncio.Task[ToolResult]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.executions = 0
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

    async def execute(
        self,
        invocation: ToolInvocation,
        context: ToolExecutionContext,
    ) -> ToolResult:
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
            _, handler = registration
            task = asyncio.create_task(self._run_handler(invocation, context, handler))
            self._tasks[invocation.invocation_id] = task
            self.executions += 1
        try:
            return await task
        except asyncio.CancelledError:
            return _result(
                invocation,
                ToolStatus.CANCELLED,
                "tool.cancelled",
                "tool invocation was cancelled",
            )
        finally:
            async with self._lock:
                if self._tasks.get(invocation.invocation_id) is task:
                    self._tasks.pop(invocation.invocation_id, None)

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
        context: ToolExecutionContext,
        handler: ToolHandler,
    ) -> ToolResult:
        try:
            value = handler(invocation.arguments, context)
            output = await value if inspect.isawaitable(value) else value
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
