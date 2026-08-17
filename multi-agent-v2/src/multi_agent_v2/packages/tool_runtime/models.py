from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from multi_agent_v2.packages.domain.json_types import JsonObject


class ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCall(ToolModel):
    call_id: str = Field(min_length=1, max_length=128)
    execution_id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=128)
    arguments: JsonObject
    timeout_seconds: float = Field(gt=0, le=3600)


class ToolError(ToolModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4096)
    retryable: bool = False


class ToolResult(ToolModel):
    call_id: str
    execution_id: str
    name: str
    status: Literal["succeeded", "failed", "denied", "cancelled", "timed_out"]
    output: JsonObject = Field(default_factory=dict)
    error: ToolError | None = None
    additional_contexts: tuple[str, ...] = ()


class ToolPreDecision(ToolModel):
    action: Literal["allow", "deny", "ask"]
    reason: str | None = Field(default=None, max_length=4096)
    call: ToolCall


class ApprovalRequest(ToolModel):
    call_id: str
    execution_id: str
    tool_name: str
    reason: str
    arguments: JsonObject


class ApprovalAnswer(ToolModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=4096)
