from __future__ import annotations

from typing import Literal

from pydantic import Field

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_dsl.ir import (
    ActivityExecutionIr,
    AgentExecutionIr,
    ExecutablePlan,
    StrictSchemaIr,
)
from multi_agent_v2.packages.workflow_runtime.state import RuntimeModel, WorkflowRuntimeState


class HistoryPolicy(RuntimeModel):
    maximum_events: int = Field(default=10_000, ge=100, le=50_000)
    maximum_bytes: int = Field(default=10_000_000, ge=1_000_000, le=40_000_000)


class WorkflowRunInput(RuntimeModel):
    plan: ExecutablePlan
    workflow_input: JsonObject
    carried_state: WorkflowRuntimeState | None = None
    generation: int = Field(default=0, ge=0)
    history_policy: HistoryPolicy = HistoryPolicy()


class NodeActivityRequest(RuntimeModel):
    workflow_instance_id: str
    plan_hash: str
    node_id: str
    activation: int
    execution_id: str
    idempotency_key: str
    resolved_inputs: JsonObject
    execution: AgentExecutionIr | ActivityExecutionIr
    output_schema: StrictSchemaIr


class NodeActivityResult(RuntimeModel):
    execution_id: str
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled"]
    output: JsonObject | None = None
    output_schema_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = None
    error_message: str | None = None


class ApprovalCommand(RuntimeModel):
    command_id: str
    node_id: str
    activation: int = Field(ge=1)
    decision: Literal["approved", "rejected"]
    operator_label: str | None = None
    reason: str | None = None


class CommandResult(RuntimeModel):
    command_id: str
    accepted: bool
    state_version: int


class WorkflowResult(RuntimeModel):
    status: Literal["succeeded", "failed", "cancelled"]
    output: JsonObject | None
    error_code: str | None
    error_message: str | None
