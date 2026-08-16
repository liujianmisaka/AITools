from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from multi_agent_v2.packages.domain.json_types import JsonObject

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
NonEmptyText = Annotated[str, Field(min_length=1, max_length=4096)]
MAX_WORKFLOW_NODES = 256
MAX_WORKFLOW_TRANSITIONS = 4_096


class DslModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: _to_camel_case(name),
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


def _to_camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _validate_non_empty_token(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must not be blank")
    if len(normalized) > 256:
        raise ValueError("value must be at most 256 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("value must not contain control characters")
    return normalized


class WorkflowMetadata(DslModel):
    id: Identifier
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)


class DagFlow(DslModel):
    type: Literal["dag"] = "dag"


class StateMachineFlow(DslModel):
    type: Literal["state_machine"] = "state_machine"
    initial_node: Identifier
    max_total_activations: int = Field(ge=1, le=100_000)
    continue_as_new_every: int | None = Field(default=None, ge=10, le=10_000)

    @model_validator(mode="after")
    def continue_threshold_must_be_below_limit(self) -> StateMachineFlow:
        if (
            self.continue_as_new_every is not None
            and self.continue_as_new_every >= self.max_total_activations
        ):
            raise ValueError("continueAsNewEvery must be lower than maxTotalActivations")
        return self


FlowDefinition = Annotated[DagFlow | StateMachineFlow, Field(discriminator="type")]


class InputBinding(DslModel):
    name: Identifier
    expression: NonEmptyText


class OutputBinding(DslModel):
    name: Identifier
    expression: NonEmptyText


class RetryDefinition(DslModel):
    maximum_attempts: int = Field(default=1, ge=1, le=10)
    initial_interval: timedelta = timedelta(seconds=1)
    maximum_interval: timedelta = timedelta(seconds=30)

    @field_validator("initial_interval", "maximum_interval")
    @classmethod
    def interval_must_be_positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("retry intervals must be positive")
        return value


class AgentDefinition(DslModel):
    provider: str
    model: str
    effort: str
    workspace_id: Identifier
    access: Literal["read_only", "workspace_write"]
    approval_mode: Literal["deny_all", "auto_review"] = "deny_all"
    network_policy: Literal["deny", "agent_default"] = "deny"
    allowed_tool_profile: str = "coding-default"
    session_mode: Literal["new", "resume"] = "new"
    provider_session_expression: str | None = Field(default=None, min_length=1, max_length=4096)
    instruction: NonEmptyText
    timeout: timedelta = timedelta(minutes=30)
    retry: RetryDefinition = RetryDefinition()

    @field_validator("provider", "model", "effort", "allowed_tool_profile")
    @classmethod
    def selection_must_be_explicit(cls, value: str) -> str:
        return _validate_non_empty_token(value)

    @field_validator("timeout")
    @classmethod
    def timeout_must_be_positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("timeout must be positive")
        return value

    @model_validator(mode="after")
    def session_reference_matches_mode(self) -> AgentDefinition:
        if self.session_mode == "resume" and self.provider_session_expression is None:
            raise ValueError("resume session mode requires providerSessionExpression")
        if self.session_mode == "new" and self.provider_session_expression is not None:
            raise ValueError("providerSessionExpression is only valid for resume session mode")
        return self


class ActivityDefinition(DslModel):
    name: Identifier
    version: int = Field(ge=1)
    timeout: timedelta = timedelta(minutes=5)
    retry: RetryDefinition = RetryDefinition(maximum_attempts=3)

    @field_validator("timeout")
    @classmethod
    def timeout_must_be_positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("timeout must be positive")
        return value


class NodeBase(DslModel):
    id: Identifier
    type_version: Literal[1] = 1
    inputs: tuple[InputBinding, ...] = ()


class AgentNode(NodeBase):
    type: Literal["agent"] = "agent"
    output_schema: JsonObject
    agent: AgentDefinition


class ActivityNode(NodeBase):
    type: Literal["activity"] = "activity"
    activity: ActivityDefinition


class DecisionNode(NodeBase):
    type: Literal["decision"] = "decision"
    expression: NonEmptyText


class ApprovalNode(NodeBase):
    type: Literal["approval"] = "approval"
    label: str = Field(min_length=1, max_length=200)
    timeout: timedelta

    @field_validator("timeout")
    @classmethod
    def timeout_must_be_positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("timeout must be positive")
        return value


class EventWaitNode(NodeBase):
    type: Literal["wait_event"] = "wait_event"
    event_type: str = Field(min_length=1, max_length=256)
    source_pattern: str | None = Field(default=None, max_length=512)
    subject_pattern: str | None = Field(default=None, max_length=512)
    correlation_expression: str | None = Field(default=None, min_length=1, max_length=4096)
    timeout: timedelta
    output_schema: JsonObject

    @field_validator("timeout")
    @classmethod
    def timeout_must_be_positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("timeout must be positive")
        return value


class TimerNode(NodeBase):
    type: Literal["timer"] = "timer"
    delay: timedelta

    @field_validator("delay")
    @classmethod
    def delay_must_be_positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("delay must be positive")
        return value


class JoinNode(NodeBase):
    type: Literal["join"] = "join"
    mode: Literal["all", "any", "quorum"]
    required: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def required_matches_mode(self) -> JoinNode:
        if self.mode == "quorum" and self.required is None:
            raise ValueError("quorum join requires a required count")
        if self.mode != "quorum" and self.required is not None:
            raise ValueError("required is only valid for quorum join")
        return self


NodeDefinition = Annotated[
    AgentNode | ActivityNode | DecisionNode | ApprovalNode | EventWaitNode | TimerNode | JoinNode,
    Field(discriminator="type"),
]


class TransitionDefinition(DslModel):
    id: Identifier
    source: Identifier = Field(alias="from")
    target: Identifier = Field(alias="to")
    on: Literal["succeeded", "failed", "cancelled", "timed_out"] = "succeeded"
    when: str | None = Field(default=None, min_length=1, max_length=4096)
    priority: int = Field(default=100, ge=0, le=1_000_000)


class WorkflowSpec(DslModel):
    flow: FlowDefinition
    input_schema: JsonObject
    output_schema: JsonObject
    failure_policy: Literal["continue_independent", "fail_fast"] = "continue_independent"
    max_concurrency: int = Field(default=4, ge=1, le=64)
    nodes: tuple[NodeDefinition, ...]
    transitions: tuple[TransitionDefinition, ...] = ()
    outputs: tuple[OutputBinding, ...] = ()

    @field_validator("nodes")
    @classmethod
    def at_least_one_node(cls, value: tuple[NodeDefinition, ...]) -> tuple[NodeDefinition, ...]:
        if not value:
            raise ValueError("workflow must contain at least one node")
        return value


class WorkflowDefinition(DslModel):
    api_version: Literal["orchestration.misaka.dev/v1"] = Field(alias="apiVersion")
    kind: Literal["Workflow"]
    metadata: WorkflowMetadata
    spec: WorkflowSpec
