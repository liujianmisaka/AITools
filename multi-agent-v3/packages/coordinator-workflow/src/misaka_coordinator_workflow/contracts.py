from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from misaka_coordinator_runtime import ExecutionPlan, ExecutionResult


class WorkflowStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    run_id: str
    node_id: str
    outputs: Mapping[str, ExecutionResult]


PlanFactory = Callable[[WorkflowContext], Awaitable[ExecutionPlan | None]]


@dataclass(frozen=True, slots=True)
class DAGNode:
    node_id: str
    plan_factory: PlanFactory
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise ValueError("node_id must not be empty")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("DAG dependencies must be unique")
        if self.node_id in self.depends_on:
            raise ValueError("DAG node cannot depend on itself")


@dataclass(frozen=True, slots=True)
class DAGDefinition:
    nodes: tuple[DAGNode, ...]

    def __post_init__(self) -> None:
        ids = [node.node_id for node in self.nodes]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("DAG node ids must be non-empty and unique")
        known = set(ids)
        for node in self.nodes:
            missing = set(node.depends_on) - known
            if missing:
                raise ValueError(
                    f"DAG node {node.node_id} has unknown dependencies: {sorted(missing)}"
                )
        _ensure_acyclic(self.nodes)


@dataclass(frozen=True, slots=True)
class WorkflowRunResult:
    run_id: str
    status: WorkflowStatus
    node_results: Mapping[str, ExecutionResult]
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class StateTransition:
    source: str
    event: str
    target: str
    plan_factory: PlanFactory | None = None

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.event.strip() or not self.target.strip():
            raise ValueError("state transition fields must not be empty")


@dataclass(frozen=True, slots=True)
class StateMachineDefinition:
    initial_state: str
    states: frozenset[str]
    transitions: tuple[StateTransition, ...]
    terminal_states: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.initial_state.strip() or self.initial_state not in self.states:
            raise ValueError("state machine initial state must be declared")
        if not self.terminal_states <= self.states:
            raise ValueError("state machine terminal states must be declared")
        keys: set[tuple[str, str]] = set()
        for transition in self.transitions:
            if transition.source not in self.states or transition.target not in self.states:
                raise ValueError("state transition references an unknown state")
            key = (transition.source, transition.event)
            if key in keys:
                raise ValueError("state machine transitions must be deterministic")
            keys.add(key)


def _empty_outputs() -> dict[str, ExecutionResult]:
    return {}


@dataclass(frozen=True, slots=True)
class StateMachineSnapshot:
    run_id: str
    state: str
    outputs: Mapping[str, ExecutionResult] = field(default_factory=_empty_outputs)


def _ensure_acyclic(nodes: tuple[DAGNode, ...]) -> None:
    dependencies = {node.node_id: set(node.depends_on) for node in nodes}
    resolved: set[str] = set()
    while dependencies:
        ready = {node_id for node_id, deps in dependencies.items() if not deps}
        if not ready:
            raise ValueError("DAG definition contains a cycle")
        resolved.update(ready)
        for node_id in ready:
            dependencies.pop(node_id)
        for deps in dependencies.values():
            deps.difference_update(ready)
