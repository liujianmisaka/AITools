from __future__ import annotations

import json

import pytest

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_dsl import compile_workflow, parse_json_workflow
from multi_agent_v2.packages.workflow_runtime.reducer import (
    WorkflowInvariantError,
    command_consumed,
    complete_node,
    initial_state,
    ready_node_ids,
    record_command,
    resolve_inputs,
    resolve_provider_session_id,
    retry_node,
    start_node,
)
from multi_agent_v2.packages.workflow_runtime.state import RuntimeErrorInfo
from tests.workflow_samples import closed_schema, copy_document, valid_context


def _compile(document: JsonObject):  # type annotation inferred from compiler boundary
    return compile_workflow(parse_json_workflow(json.dumps(document)), valid_context())


def test_sequential_dag_reduces_to_output() -> None:
    plan = _compile(copy_document())
    workflow_input: JsonObject = {"formula": "1 + 2"}
    state = initial_state(plan)

    assert ready_node_ids(plan, state, workflow_input) == ("extract",)
    state = start_node(plan, state, "extract")
    extract = next(node for node in plan.nodes if node.id == "extract")
    assert resolve_inputs(extract, state, workflow_input) == {"formula": "1 + 2"}
    state = complete_node(
        plan,
        state,
        workflow_input,
        "extract",
        "succeeded",
        output={"formula": "1 + 2"},
    )

    assert ready_node_ids(plan, state, workflow_input) == ("calculate",)
    state = start_node(plan, state, "calculate")
    state = complete_node(
        plan,
        state,
        workflow_input,
        "calculate",
        "succeeded",
        output={"result": 3},
    )

    assert state.status == "succeeded"
    assert state.result == {"result": 3}
    assert state.total_activations == 2


def test_condition_not_taken_skips_dependent_node() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    transitions = spec["transitions"]
    assert isinstance(transitions, list)
    edge = transitions[0]
    assert isinstance(edge, dict)
    edge["when"] = "nodes.extract.output.valid == `true`"
    nodes = spec["nodes"]
    assert isinstance(nodes, list)
    extract = nodes[0]
    assert isinstance(extract, dict)
    extract["outputSchema"] = closed_schema(formula={"type": "string"}, valid={"type": "boolean"})
    plan = _compile(document)
    workflow_input: JsonObject = {"formula": "1 + 2"}
    state = start_node(plan, initial_state(plan), "extract")
    state = complete_node(
        plan,
        state,
        workflow_input,
        "extract",
        "succeeded",
        output={"formula": "1 + 2", "valid": False},
    )

    assert state.status == "succeeded"
    assert next(item for item in state.nodes if item.node_id == "calculate").status == "skipped"


def test_fail_fast_stops_new_activations() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    spec["failurePolicy"] = "fail_fast"
    plan = _compile(document)
    workflow_input: JsonObject = {"formula": "bad"}
    state = start_node(plan, initial_state(plan), "extract")
    state = complete_node(
        plan,
        state,
        workflow_input,
        "extract",
        "failed",
        error=RuntimeErrorInfo(code="fake.failed", message="fake failure"),
    )

    assert state.status == "failed"
    assert next(item for item in state.nodes if item.node_id == "calculate").status == "skipped"


def test_state_machine_selects_priority_and_reaches_terminal() -> None:
    document = _state_machine_document()
    plan = _compile(document)
    workflow_input: JsonObject = {"formula": "1 + 2"}
    state = initial_state(plan)

    assert ready_node_ids(plan, state, workflow_input) == ("choose",)
    state = start_node(plan, state, "choose")
    state = complete_node(
        plan,
        state,
        workflow_input,
        "choose",
        "succeeded",
        output={"result": True},
    )
    assert state.current_node_id == "calculate"
    state = start_node(plan, state, "calculate")
    state = complete_node(
        plan,
        state,
        workflow_input,
        "calculate",
        "succeeded",
        output={"result": 3},
    )

    assert state.status == "succeeded"
    assert state.result == {"result": 3}


def test_activation_limit_fails_state_machine() -> None:
    document = _state_machine_document(max_total_activations=1)
    plan = _compile(document)
    workflow_input: JsonObject = {"formula": "1 + 2"}
    state = start_node(plan, initial_state(plan), "choose")
    state = complete_node(
        plan,
        state,
        workflow_input,
        "choose",
        "succeeded",
        output={"result": True},
    )
    state = start_node(plan, state, "calculate")

    assert state.status == "failed"
    assert state.error is not None
    assert state.error.code == "workflow.activation_limit_exceeded"


def test_retry_and_command_deduplication_are_stable() -> None:
    plan = _compile(copy_document())
    state = start_node(plan, initial_state(plan), "extract")
    state = complete_node(
        plan,
        state,
        {"formula": "bad"},
        "extract",
        "failed",
        error=RuntimeErrorInfo(code="fake.failed", message="fake failure"),
    )
    state = retry_node(state, "extract")
    assert next(item for item in state.nodes if item.node_id == "extract").status == "pending"

    recorded = record_command(state, "command-1")
    assert command_consumed(recorded, "command-1")
    assert record_command(recorded, "command-1") is recorded
    with pytest.raises(WorkflowInvariantError):
        retry_node(recorded, "calculate")


def test_resume_session_expression_resolves_from_instance_input() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, list)
    agent_node = nodes[0]
    assert isinstance(agent_node, dict)
    agent = agent_node["agent"]
    assert isinstance(agent, dict)
    agent["sessionMode"] = "resume"
    agent["providerSessionExpression"] = "workflow.input.sessionId"
    input_schema = spec["inputSchema"]
    assert isinstance(input_schema, dict)
    properties = input_schema["properties"]
    assert isinstance(properties, dict)
    properties["sessionId"] = {"type": "string"}
    plan = _compile(document)
    node = next(item for item in plan.nodes if item.id == "extract")

    session_id = resolve_provider_session_id(
        node,
        initial_state(plan),
        {"formula": "1 + 2", "sessionId": "codex-thread-1"},
    )

    assert session_id == "codex-thread-1"


def test_reconciliation_required_is_not_collapsed_into_failure() -> None:
    plan = _compile(copy_document())
    state = start_node(plan, initial_state(plan), "extract")
    state = complete_node(
        plan,
        state,
        {"formula": "1 + 2"},
        "extract",
        "reconciliation_required",
        error=RuntimeErrorInfo(
            code="agent.reconciliation_required",
            message="agent execution requires reconciliation",
        ),
    )

    assert state.status == "attention_required"
    assert state.error is not None
    assert state.error.code == "agent.reconciliation_required"


def _state_machine_document(*, max_total_activations: int = 10) -> JsonObject:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    spec["flow"] = {
        "type": "state_machine",
        "initialNode": "choose",
        "maxTotalActivations": max_total_activations,
    }
    spec["nodes"] = [
        {
            "id": "choose",
            "type": "decision",
            "expression": "workflow.input.formula != null",
        },
        {
            "id": "calculate",
            "type": "activity",
            "inputs": [{"name": "formula", "expression": "workflow.input.formula"}],
            "activity": {"name": "calculate", "version": 1},
        },
    ]
    spec["transitions"] = [
        {
            "id": "choose-calculate",
            "from": "choose",
            "to": "calculate",
            "on": "succeeded",
            "when": "nodes.choose.output.result == `true`",
            "priority": 10,
        },
        {
            "id": "choose-fallback",
            "from": "choose",
            "to": "calculate",
            "on": "succeeded",
            "priority": 100,
        },
    ]
    return document
