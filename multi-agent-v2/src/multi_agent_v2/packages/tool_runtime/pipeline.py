from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from jsonschema import Draft202012Validator

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.tool_runtime.models import (
    ApprovalAnswer,
    ApprovalRequest,
    ToolCall,
    ToolError,
    ToolPreDecision,
    ToolResult,
)


class ToolDefinition(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def output_schema(self) -> JsonObject: ...

    async def execute(self, call: ToolCall) -> JsonObject: ...


class ToolPreHook(Protocol):
    async def apply(self, call: ToolCall) -> ToolPreDecision: ...


class ToolGuard(Protocol):
    async def evaluate(self, call: ToolCall) -> str | None: ...


class ToolPostHook(Protocol):
    async def apply(self, call: ToolCall, result: ToolResult) -> ToolResult: ...


class ApprovalProvider(Protocol):
    async def ask(self, request: ApprovalRequest) -> ApprovalAnswer: ...


class ToolAuditSink(Protocol):
    async def record(self, call: ToolCall, result: ToolResult) -> None: ...


@dataclass(frozen=True, slots=True)
class FunctionTool:
    name: str
    output_schema: JsonObject
    function: Callable[[ToolCall], Awaitable[JsonObject]]

    async def execute(self, call: ToolCall) -> JsonObject:
        return await self.function(call)


class ToolPipelineError(RuntimeError):
    pass


class ToolPipeline:
    def __init__(
        self,
        *,
        approval: ApprovalProvider | None = None,
        audit: ToolAuditSink | None = None,
    ) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._pre_hooks: list[ToolPreHook] = []
        self._guards: list[ToolGuard] = []
        self._post_hooks: list[ToolPostHook] = []
        self._approval = approval
        self._audit = audit

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ToolPipelineError(f"tool is already registered: {definition.name}")
        Draft202012Validator.check_schema(definition.output_schema)
        self._definitions[definition.name] = definition

    def add_pre_hook(self, hook: ToolPreHook) -> None:
        self._pre_hooks.append(hook)

    def add_guard(self, guard: ToolGuard) -> None:
        self._guards.append(guard)

    def add_post_hook(self, hook: ToolPostHook) -> None:
        self._post_hooks.append(hook)

    async def execute(self, call: ToolCall) -> ToolResult:
        current = call.model_copy(deep=True)
        denied_reason: str | None = None
        approval_reason: str | None = None
        for hook in tuple(self._pre_hooks):
            decision = await hook.apply(current)
            if (
                decision.call.call_id != current.call_id
                or decision.call.execution_id != current.execution_id
                or decision.call.name != current.name
            ):
                raise ToolPipelineError("pre hook cannot change tool call identity")
            current = decision.call.model_copy(deep=True)
            if decision.action == "deny":
                denied_reason = decision.reason or "tool call was denied"
                break
            if decision.action == "ask":
                approval_reason = decision.reason or "tool call requires approval"

        if denied_reason is None and approval_reason is not None:
            denied_reason = await self._resolve_approval(current, approval_reason)
        if denied_reason is None:
            for guard in tuple(self._guards):
                reason = await guard.evaluate(current)
                if reason is not None:
                    denied_reason = reason
                    break

        if denied_reason is not None:
            result = ToolResult(
                call_id=current.call_id,
                execution_id=current.execution_id,
                name=current.name,
                status="denied",
                error=ToolError(code="tool.denied", message=denied_reason),
            )
            return await self._finalize(current, result)

        definition = self._definitions.get(current.name)
        if definition is None:
            result = ToolResult(
                call_id=current.call_id,
                execution_id=current.execution_id,
                name=current.name,
                status="failed",
                error=ToolError(code="tool.unknown", message="tool is not registered"),
            )
            return await self._finalize(current, result)

        try:
            async with asyncio.timeout(current.timeout_seconds):
                output = await definition.execute(current)
            errors = tuple(
                Draft202012Validator(definition.output_schema).iter_errors(  # pyright: ignore[reportUnknownMemberType]
                    output
                )
            )
            if errors:
                raise ToolPipelineError(f"tool output violates its schema: {errors[0].message}")
            result = ToolResult(
                call_id=current.call_id,
                execution_id=current.execution_id,
                name=current.name,
                status="succeeded",
                output=output,
            )
        except TimeoutError:
            result = ToolResult(
                call_id=current.call_id,
                execution_id=current.execution_id,
                name=current.name,
                status="timed_out",
                error=ToolError(code="tool.timed_out", message="tool execution timed out"),
            )
        except asyncio.CancelledError:
            result = ToolResult(
                call_id=current.call_id,
                execution_id=current.execution_id,
                name=current.name,
                status="cancelled",
                error=ToolError(code="tool.cancelled", message="tool execution was cancelled"),
            )
        except Exception as exc:
            code = getattr(exc, "code", "tool.failed")
            result = ToolResult(
                call_id=current.call_id,
                execution_id=current.execution_id,
                name=current.name,
                status="failed",
                error=ToolError(code=str(code), message=str(exc) or type(exc).__name__),
            )
        return await self._finalize(current, result)

    async def _resolve_approval(self, call: ToolCall, reason: str) -> str | None:
        if self._approval is None:
            return "approval provider is unavailable"
        try:
            answer = await self._approval.ask(
                ApprovalRequest(
                    call_id=call.call_id,
                    execution_id=call.execution_id,
                    tool_name=call.name,
                    reason=reason,
                    arguments=call.arguments,
                )
            )
        except (asyncio.CancelledError, Exception) as exc:
            return f"approval could not be obtained: {type(exc).__name__}"
        if not answer.approved:
            return answer.reason or "approval was declined"
        return None

    async def _finalize(self, call: ToolCall, initial: ToolResult) -> ToolResult:
        result = initial
        protected_status = initial.status if initial.status != "succeeded" else None
        for hook in tuple(self._post_hooks):
            candidate = await hook.apply(call, result)
            if (
                candidate.call_id != call.call_id
                or candidate.execution_id != call.execution_id
                or candidate.name != call.name
            ):
                raise ToolPipelineError("post hook cannot change tool result identity")
            if protected_status is not None and candidate.status == "succeeded":
                raise ToolPipelineError("post hook cannot turn a non-success into success")
            result = candidate.model_copy(deep=True)
        if self._audit is not None:
            await self._audit.record(call, result)
        return result
