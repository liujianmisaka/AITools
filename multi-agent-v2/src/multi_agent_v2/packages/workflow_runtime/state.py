from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from multi_agent_v2.packages.domain.json_types import JsonObject

type NodeStatus = Literal[
    "pending",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "skipped",
]
type WorkflowStatus = Literal["running", "succeeded", "failed", "cancelled"]
type TerminalNodeStatus = Literal["succeeded", "failed", "timed_out", "cancelled", "skipped"]


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeErrorInfo(RuntimeModel):
    code: str
    message: str


class NodeRuntimeState(RuntimeModel):
    node_id: str
    status: NodeStatus = "pending"
    activation: int = 0
    output: JsonObject | None = None
    error: RuntimeErrorInfo | None = None


class ConsumedCommand(RuntimeModel):
    command_id: str
    fingerprint: str


class WorkflowRuntimeState(RuntimeModel):
    status: WorkflowStatus = "running"
    state_version: int = 0
    generation: int = 0
    total_activations: int = 0
    current_node_id: str | None = None
    nodes: tuple[NodeRuntimeState, ...]
    consumed_commands: tuple[ConsumedCommand, ...] = ()
    result: JsonObject | None = None
    error: RuntimeErrorInfo | None = None


class WorkflowSnapshot(RuntimeModel):
    status: WorkflowStatus
    state_version: int
    generation: int
    total_activations: int
    current_node_id: str | None
    nodes: tuple[NodeRuntimeState, ...]
    result: JsonObject | None
    error: RuntimeErrorInfo | None
