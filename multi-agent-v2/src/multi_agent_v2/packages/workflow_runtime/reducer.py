from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue
from multi_agent_v2.packages.workflow_dsl.expressions import (
    evaluate_condition,
    evaluate_expression,
)
from multi_agent_v2.packages.workflow_dsl.ir import (
    ExecutablePlan,
    JoinExecutionIr,
    NodeIr,
    TransitionIr,
)
from multi_agent_v2.packages.workflow_runtime.state import (
    ConsumedCommand,
    NodeRuntimeState,
    RuntimeErrorInfo,
    TerminalNodeStatus,
    WorkflowRuntimeState,
    WorkflowSnapshot,
)

_TERMINAL_NODE_STATUSES = {
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "skipped",
    "reconciliation_required",
}
_TRANSITION_OUTCOME = {
    "succeeded": "succeeded",
    "failed": "failed",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
    "skipped": None,
    "reconciliation_required": None,
}
_COMMAND_WINDOW = 512


class WorkflowInvariantError(RuntimeError):
    pass


def initial_state(plan: ExecutablePlan, *, generation: int = 0) -> WorkflowRuntimeState:
    return WorkflowRuntimeState(
        generation=generation,
        current_node_id=plan.initial_node_id,
        nodes=tuple(NodeRuntimeState(node_id=node.id) for node in plan.nodes),
    )


def snapshot(state: WorkflowRuntimeState) -> WorkflowSnapshot:
    return WorkflowSnapshot(
        status=state.status,
        state_version=state.state_version,
        generation=state.generation,
        total_activations=state.total_activations,
        current_node_id=state.current_node_id,
        nodes=state.nodes,
        result=state.result,
        error=state.error,
    )


def ready_node_ids(
    plan: ExecutablePlan,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
) -> tuple[str, ...]:
    if state.status != "running":
        return ()
    states = _state_map(state)
    if plan.mode == "state_machine":
        current = state.current_node_id
        if current is None:
            return ()
        return (current,) if states[current].status == "pending" else ()

    ready: list[str] = []
    for node in plan.nodes:
        if states[node.id].status != "pending":
            continue
        incoming = _incoming(plan, node.id)
        if not incoming:
            ready.append(node.id)
            continue
        selected_sources = {
            transition.source
            for transition in incoming
            if _transition_selected(transition, state, workflow_input)
        }
        if isinstance(node.execution, JoinExecutionIr):
            incoming_sources = {transition.source for transition in incoming}
            required = _join_required(node.execution, len(incoming_sources))
            if len(selected_sources) >= required:
                ready.append(node.id)
            continue
        if not all(
            states[transition.source].status in _TERMINAL_NODE_STATUSES for transition in incoming
        ):
            continue
        if len(selected_sources) == len({transition.source for transition in incoming}):
            ready.append(node.id)
    return tuple(sorted(ready))


def settle_dag(
    plan: ExecutablePlan,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
) -> WorkflowRuntimeState:
    if plan.mode != "dag" or state.status != "running":
        return state
    current = state
    while True:
        states = _state_map(current)
        updates: dict[str, NodeRuntimeState] = {}
        for node in plan.nodes:
            node_state = states[node.id]
            if node_state.status != "pending":
                continue
            incoming = _incoming(plan, node.id)
            if not incoming or not all(
                states[edge.source].status in _TERMINAL_NODE_STATUSES for edge in incoming
            ):
                continue
            selected_sources = {
                edge.source
                for edge in incoming
                if _transition_selected(edge, current, workflow_input)
            }
            incoming_sources = {edge.source for edge in incoming}
            if isinstance(node.execution, JoinExecutionIr):
                required = _join_required(node.execution, len(incoming_sources))
                if len(selected_sources) < required:
                    updates[node.id] = node_state.model_copy(
                        update={
                            "status": "failed",
                            "error": RuntimeErrorInfo(
                                code="join.requirement_unmet",
                                message="join requirement cannot be satisfied",
                            ),
                        }
                    )
            elif len(selected_sources) != len(incoming_sources):
                updates[node.id] = node_state.model_copy(update={"status": "skipped"})
        if not updates:
            break
        current = _replace_nodes(current, updates)

    states = _state_map(current)
    if plan.failure_policy == "fail_fast" and any(
        item.status in {"failed", "timed_out", "cancelled", "reconciliation_required"}
        for item in states.values()
    ):
        pending = {
            node_id: item.model_copy(update={"status": "skipped"})
            for node_id, item in states.items()
            if item.status == "pending"
        }
        current = _replace_nodes(current, pending)
        failures = sorted(
            (
                item
                for item in _state_map(current).values()
                if item.status in {"failed", "timed_out", "cancelled", "reconciliation_required"}
            ),
            key=lambda item: (item.status != "reconciliation_required", item.node_id),
        )
        first = failures[0]
        return _bump(
            current,
            status=(
                "attention_required" if first.status == "reconciliation_required" else "failed"
            ),
            error=first.error
            or RuntimeErrorInfo(
                code=f"node.{first.status}",
                message=f"node {first.node_id} {first.status}",
            ),
        )

    states = _state_map(current)
    if all(item.status in _TERMINAL_NODE_STATUSES for item in states.values()):
        failures = [
            item
            for item in states.values()
            if item.status in {"failed", "timed_out", "cancelled", "reconciliation_required"}
        ]
        if failures:
            first = sorted(
                failures,
                key=lambda item: (item.status != "reconciliation_required", item.node_id),
            )[0]
            error = first.error or RuntimeErrorInfo(
                code=f"node.{first.status}", message=f"node {first.node_id} {first.status}"
            )
            current = _bump(
                current,
                status=(
                    "attention_required" if first.status == "reconciliation_required" else "failed"
                ),
                error=error,
            )
        else:
            current = _bump(
                current,
                status="succeeded",
                result=_workflow_output(plan, current, workflow_input),
            )
    return current


def start_node(
    plan: ExecutablePlan,
    state: WorkflowRuntimeState,
    node_id: str,
    *,
    waiting_status: Literal["waiting_approval", "waiting_event"] | None = None,
) -> WorkflowRuntimeState:
    node_state = _get_node(state, node_id)
    if node_state.status != "pending":
        raise WorkflowInvariantError(f"node {node_id} is not pending")
    if (
        plan.max_total_activations is not None
        and state.total_activations >= plan.max_total_activations
    ):
        return _bump(
            state,
            status="failed",
            error=RuntimeErrorInfo(
                code="workflow.activation_limit_exceeded",
                message="workflow activation limit exceeded",
            ),
        )
    updated = node_state.model_copy(
        update={
            "status": waiting_status or "running",
            "activation": node_state.activation + 1,
            "output": None,
            "error": None,
        }
    )
    return _replace_nodes(
        state.model_copy(update={"total_activations": state.total_activations + 1}),
        {node_id: updated},
    )


def complete_node(
    plan: ExecutablePlan,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
    node_id: str,
    outcome: TerminalNodeStatus,
    *,
    output: JsonObject | None = None,
    error: RuntimeErrorInfo | None = None,
) -> WorkflowRuntimeState:
    node_state = _get_node(state, node_id)
    if node_state.status not in {"running", "waiting_approval", "waiting_event"}:
        raise WorkflowInvariantError(f"node {node_id} is not active")
    if outcome == "succeeded" and output is None:
        output = {}
    updated = node_state.model_copy(update={"status": outcome, "output": output, "error": error})
    current = _replace_nodes(state, {node_id: updated})
    if plan.mode == "state_machine":
        return _advance_state_machine(plan, current, workflow_input, node_id, outcome)
    return settle_dag(plan, current, workflow_input)


def retry_node(state: WorkflowRuntimeState, node_id: str) -> WorkflowRuntimeState:
    node_state = _get_node(state, node_id)
    if node_state.status not in {"failed", "timed_out", "cancelled"}:
        raise WorkflowInvariantError(f"node {node_id} cannot be retried")
    return _replace_nodes(
        state.model_copy(update={"status": "running", "error": None, "result": None}),
        {
            node_id: node_state.model_copy(
                update={"status": "pending", "output": None, "error": None}
            )
        },
    )


def cancel_workflow(
    state: WorkflowRuntimeState,
    *,
    reason: str,
) -> WorkflowRuntimeState:
    if state.status != "running":
        return state
    updates: dict[str, NodeRuntimeState] = {}
    for node in state.nodes:
        if node.status in {"running", "waiting_approval", "waiting_event"}:
            updates[node.node_id] = node.model_copy(
                update={
                    "status": "cancelled",
                    "error": RuntimeErrorInfo(
                        code="workflow.cancelled",
                        message=reason,
                    ),
                }
            )
        elif node.status == "pending":
            updates[node.node_id] = node.model_copy(update={"status": "skipped"})
    current = _replace_nodes(state, updates)
    return _bump(
        current,
        status="cancelled",
        error=RuntimeErrorInfo(code="workflow.cancelled", message=reason),
    )


def skip_node(
    plan: ExecutablePlan,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
    node_id: str,
) -> WorkflowRuntimeState:
    node_state = _get_node(state, node_id)
    if node_state.status not in {"pending", "failed", "timed_out", "cancelled"}:
        raise WorkflowInvariantError(f"node {node_id} cannot be skipped")
    current = _replace_nodes(
        state,
        {node_id: node_state.model_copy(update={"status": "skipped", "output": None})},
    )
    if plan.mode == "state_machine":
        return _bump(current, status="succeeded", current_node_id=None)
    return settle_dag(plan, current, workflow_input)


def record_command(
    state: WorkflowRuntimeState,
    command_id: str,
    fingerprint: str = "",
) -> WorkflowRuntimeState:
    if command_consumed(state, command_id):
        return state
    commands = (
        *state.consumed_commands,
        ConsumedCommand(command_id=command_id, fingerprint=fingerprint),
    )[-_COMMAND_WINDOW:]
    return _bump(state, consumed_commands=commands)


def command_consumed(state: WorkflowRuntimeState, command_id: str) -> bool:
    return any(item.command_id == command_id for item in state.consumed_commands)


def command_fingerprint(state: WorkflowRuntimeState, command_id: str) -> str | None:
    return next(
        (item.fingerprint for item in state.consumed_commands if item.command_id == command_id),
        None,
    )


def resolve_inputs(
    node: NodeIr,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
) -> JsonObject:
    context = _expression_context(state, workflow_input, current_node_id=node.id)
    return {
        binding.name: evaluate_expression(binding.expression, context) for binding in node.inputs
    }


def resolve_provider_session_id(
    node: NodeIr,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
) -> str | None:
    if node.execution.kind != "agent":
        return None
    expression = node.execution.provider_session_expression
    if expression is None:
        if node.execution.session_mode == "resume":
            raise WorkflowInvariantError("resume agent has no provider session expression")
        return None
    value = evaluate_expression(
        expression,
        _expression_context(state, workflow_input, current_node_id=node.id),
    )
    if not isinstance(value, str) or not value.strip():
        raise ValueError("provider session expression must return a non-empty string")
    return value.strip()


def resolve_event_correlation_key(
    node: NodeIr,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
) -> str | None:
    if node.execution.kind != "wait_event":
        return None
    expression = node.execution.correlation_expression
    if expression is None:
        return None
    value = evaluate_expression(
        expression,
        _expression_context(state, workflow_input, current_node_id=node.id),
    )
    if not isinstance(value, str) or not value.strip():
        raise ValueError("event correlation expression must return a non-empty string")
    return value.strip()


def evaluate_node_decision(
    node: NodeIr,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
) -> bool:
    if node.execution.kind != "decision":
        raise WorkflowInvariantError(f"node {node.id} is not a decision")
    return evaluate_condition(
        node.execution.expression,
        _expression_context(state, workflow_input, current_node_id=node.id),
    )


def join_output(
    plan: ExecutablePlan,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
    node_id: str,
) -> JsonObject:
    selected = {
        transition.source
        for transition in _incoming(plan, node_id)
        if _transition_selected(transition, state, workflow_input)
    }
    completed: list[JsonValue] = [item for item in sorted(selected)]
    return {"completed": completed}


def _advance_state_machine(
    plan: ExecutablePlan,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
    node_id: str,
    outcome: TerminalNodeStatus,
) -> WorkflowRuntimeState:
    matching = [
        transition
        for transition in plan.transitions
        if transition.source == node_id and transition.on == _TRANSITION_OUTCOME[outcome]
    ]
    matching.sort(key=lambda item: (item.priority, item.target, item.id))
    selected = next(
        (
            transition
            for transition in matching
            if transition.when is None
            or evaluate_condition(
                transition.when,
                _expression_context(state, workflow_input, current_node_id=node_id),
            )
        ),
        None,
    )
    if selected is None:
        if outcome == "succeeded":
            return _bump(
                state,
                status="succeeded",
                current_node_id=None,
                result=_workflow_output(plan, state, workflow_input),
            )
        error = _get_node(state, node_id).error or RuntimeErrorInfo(
            code=f"node.{outcome}", message=f"node {node_id} {outcome}"
        )
        return _bump(
            state,
            status=("attention_required" if outcome == "reconciliation_required" else "failed"),
            current_node_id=None,
            error=error,
        )
    target = _get_node(state, selected.target).model_copy(
        update={"status": "pending", "output": None, "error": None}
    )
    return _replace_nodes(
        state.model_copy(update={"current_node_id": selected.target}),
        {selected.target: target},
    )


def _transition_selected(
    transition: TransitionIr,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
) -> bool:
    source = _get_node(state, transition.source)
    if _TRANSITION_OUTCOME.get(source.status) != transition.on:
        return False
    if transition.when is None:
        return True
    return evaluate_condition(
        transition.when,
        _expression_context(state, workflow_input, current_node_id=transition.source),
    )


def _expression_context(
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
    *,
    current_node_id: str,
) -> JsonObject:
    current = _get_node(state, current_node_id)
    nodes: JsonObject = {}
    for node in state.nodes:
        nodes[node.node_id] = {
            "status": node.status,
            "output": node.output,
            "error": node.error.model_dump(mode="json") if node.error else None,
            "activation": node.activation,
        }
    return {
        "workflow": {"input": workflow_input},
        "nodes": nodes,
        "current": {"nodeId": current_node_id, "activation": current.activation},
    }


def _workflow_output(
    plan: ExecutablePlan,
    state: WorkflowRuntimeState,
    workflow_input: JsonObject,
) -> JsonObject:
    context = _expression_context(
        state,
        workflow_input,
        current_node_id=state.nodes[0].node_id,
    )
    return {
        binding.name: evaluate_expression(binding.expression, context)
        for binding in plan.output_bindings
    }


def _join_required(execution: JoinExecutionIr, incoming_count: int) -> int:
    if execution.mode == "all":
        return incoming_count
    if execution.mode == "any":
        return 1
    assert execution.required is not None
    return execution.required


def _incoming(plan: ExecutablePlan, node_id: str) -> list[TransitionIr]:
    return [transition for transition in plan.transitions if transition.target == node_id]


def _state_map(state: WorkflowRuntimeState) -> dict[str, NodeRuntimeState]:
    return {item.node_id: item for item in state.nodes}


def _get_node(state: WorkflowRuntimeState, node_id: str) -> NodeRuntimeState:
    for item in state.nodes:
        if item.node_id == node_id:
            return item
    raise WorkflowInvariantError(f"unknown node {node_id}")


def _replace_nodes(
    state: WorkflowRuntimeState,
    updates: dict[str, NodeRuntimeState],
) -> WorkflowRuntimeState:
    if not updates:
        return state
    nodes = tuple(updates.get(item.node_id, item) for item in state.nodes)
    return _bump(state, nodes=nodes)


def _bump(state: WorkflowRuntimeState, **changes: JsonValue | object) -> WorkflowRuntimeState:
    return state.model_copy(update={"state_version": state.state_version + 1, **changes})


def terminal_node_ids(states: Iterable[NodeRuntimeState]) -> tuple[str, ...]:
    return tuple(sorted(item.node_id for item in states if item.status in _TERMINAL_NODE_STATUSES))
