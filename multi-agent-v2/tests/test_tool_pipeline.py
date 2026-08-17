from __future__ import annotations

from dataclasses import dataclass

import pytest

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.tool_runtime import (
    ApprovalAnswer,
    ApprovalRequest,
    FunctionTool,
    ToolCall,
    ToolPipeline,
    ToolPipelineError,
    ToolPreDecision,
    ToolResult,
)


def _call() -> ToolCall:
    return ToolCall(
        call_id="call-1",
        execution_id="execution-1",
        name="sum",
        arguments={"left": 2, "right": 3},
        timeout_seconds=1,
    )


@dataclass
class _PreHook:
    action: str

    async def apply(self, call: ToolCall) -> ToolPreDecision:
        return ToolPreDecision(
            action=self.action,  # type: ignore[arg-type]
            reason="review the calculation",
            call=call,
        )


@dataclass
class _Guard:
    reason: str | None

    async def evaluate(self, call: ToolCall) -> str | None:
        del call
        return self.reason


@dataclass
class _Approval:
    approved: bool

    async def ask(self, request: ApprovalRequest) -> ApprovalAnswer:
        assert request.call_id == "call-1"
        return ApprovalAnswer(approved=self.approved)


class _PromoteFailure:
    async def apply(self, call: ToolCall, result: ToolResult) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            execution_id=call.execution_id,
            name=call.name,
            status="succeeded",
            output={"value": 5},
        )


class _ChangeIdentity:
    async def apply(self, call: ToolCall) -> ToolPreDecision:
        return ToolPreDecision(
            action="allow",
            call=call.model_copy(update={"name": "different"}),
        )


@pytest.mark.asyncio
async def test_pipeline_executes_and_validates_structured_output() -> None:
    pipeline = ToolPipeline()

    async def execute(call: ToolCall) -> JsonObject:
        left = call.arguments["left"]
        right = call.arguments["right"]
        assert isinstance(left, int) and not isinstance(left, bool)
        assert isinstance(right, int) and not isinstance(right, bool)
        return {"value": left + right}

    pipeline.register(
        FunctionTool(
            name="sum",
            output_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            function=execute,
        )
    )

    result = await pipeline.execute(_call())

    assert result.status == "succeeded"
    assert result.output == {"value": 5}


@pytest.mark.asyncio
async def test_denial_is_monotonic_and_skips_the_tool_body() -> None:
    called = False
    pipeline = ToolPipeline()
    pipeline.add_guard(_Guard("workspace policy denied the call"))

    async def execute(_call: ToolCall) -> JsonObject:
        nonlocal called
        called = True
        return {"called": True}

    pipeline.register(
        FunctionTool(
            name="sum",
            output_schema={"type": "object"},
            function=execute,
        )
    )

    result = await pipeline.execute(_call())

    assert result.status == "denied"
    assert not called


@pytest.mark.asyncio
async def test_missing_or_declined_approval_fails_closed() -> None:
    without_provider = ToolPipeline()
    without_provider.add_pre_hook(_PreHook("ask"))
    declined = ToolPipeline(approval=_Approval(False))
    declined.add_pre_hook(_PreHook("ask"))

    missing = await without_provider.execute(_call())
    rejected = await declined.execute(_call())

    assert missing.status == "denied"
    assert rejected.status == "denied"


@pytest.mark.asyncio
async def test_post_hook_cannot_promote_a_denial_to_success() -> None:
    pipeline = ToolPipeline()
    pipeline.add_guard(_Guard("denied"))
    pipeline.add_post_hook(_PromoteFailure())

    with pytest.raises(ToolPipelineError, match="non-success"):
        await pipeline.execute(_call())


@pytest.mark.asyncio
async def test_pre_hook_cannot_redirect_the_call_to_another_tool() -> None:
    pipeline = ToolPipeline()
    pipeline.add_pre_hook(_ChangeIdentity())

    with pytest.raises(ToolPipelineError, match="identity"):
        await pipeline.execute(_call())
