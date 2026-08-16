from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class IrModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrictSchemaIr(IrModel):
    canonical: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BindingIr(IrModel):
    name: str
    expression: str


class RetryIr(IrModel):
    maximum_attempts: int
    initial_interval_ms: int
    maximum_interval_ms: int


class AgentExecutionIr(IrModel):
    kind: Literal["agent"] = "agent"
    provider: str
    model: str
    effort: str
    workspace_id: str
    access: Literal["read_only", "workspace_write"]
    session_mode: Literal["new", "resume"]
    instruction: str
    timeout_ms: int
    retry: RetryIr


class ActivityExecutionIr(IrModel):
    kind: Literal["activity"] = "activity"
    name: str
    version: int
    timeout_ms: int
    retry: RetryIr


class DecisionExecutionIr(IrModel):
    kind: Literal["decision"] = "decision"
    expression: str


class ApprovalExecutionIr(IrModel):
    kind: Literal["approval"] = "approval"
    label: str
    timeout_ms: int


class TimerExecutionIr(IrModel):
    kind: Literal["timer"] = "timer"
    delay_ms: int


class JoinExecutionIr(IrModel):
    kind: Literal["join"] = "join"
    mode: Literal["all", "any", "quorum"]
    required: int | None


ExecutionIr = Annotated[
    AgentExecutionIr
    | ActivityExecutionIr
    | DecisionExecutionIr
    | ApprovalExecutionIr
    | TimerExecutionIr
    | JoinExecutionIr,
    Field(discriminator="kind"),
]


class NodeIr(IrModel):
    id: str
    type_version: int
    inputs: tuple[BindingIr, ...]
    output_schema: StrictSchemaIr
    execution: ExecutionIr


class TransitionIr(IrModel):
    id: str
    source: str
    target: str
    on: Literal["succeeded", "failed", "cancelled", "timed_out"]
    when: str | None
    priority: int


class ExecutablePlan(IrModel):
    ir_version: Literal[1] = 1
    workflow_id: str
    workflow_version: int
    mode: Literal["dag", "state_machine"]
    failure_policy: Literal["continue_independent", "fail_fast"]
    max_concurrency: int
    initial_node_id: str | None
    max_total_activations: int | None
    continue_as_new_every: int | None
    input_schema: StrictSchemaIr
    output_schema: StrictSchemaIr
    nodes: tuple[NodeIr, ...]
    transitions: tuple[TransitionIr, ...]
    output_bindings: tuple[BindingIr, ...]
    catalog_revision: str
    compiler_version: str
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
