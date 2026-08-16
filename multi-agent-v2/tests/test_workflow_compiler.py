from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue
from multi_agent_v2.packages.workflow_dsl import (
    CompilationContext,
    ExecutablePlan,
    RegisteredActivity,
    WorkflowCompilationError,
    compile_workflow,
    parse_json_workflow,
)
from multi_agent_v2.packages.workflow_dsl.models import (
    MAX_WORKFLOW_NODES,
    MAX_WORKFLOW_TRANSITIONS,
)
from tests.workflow_samples import closed_schema, copy_document, valid_context


def _compile(document: JsonObject) -> ExecutablePlan:
    return compile_workflow(parse_json_workflow(json.dumps(document)), valid_context())


def test_compiler_creates_sorted_deeply_immutable_plan() -> None:
    plan = _compile(copy_document())

    assert [node.id for node in plan.nodes] == ["calculate", "extract"]
    assert plan.nodes[1].execution.kind == "agent"
    assert plan.nodes[1].execution.model == "sensenova/deepseek-v4-flash"
    assert plan.nodes[1].execution.effort == "high"
    assert len(plan.plan_hash) == 64
    assert ExecutablePlan.model_validate_json(plan.model_dump_json()) == plan
    with pytest.raises(ValidationError):
        plan.nodes = plan.nodes  # pyright: ignore[reportAttributeAccessIssue]


def test_equivalent_node_order_produces_same_hash() -> None:
    original = copy_document()
    reordered = copy_document()
    spec = reordered["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, list)
    nodes.reverse()

    assert _compile(original).plan_hash == _compile(reordered).plan_hash


def test_semantic_change_produces_different_hash() -> None:
    original = copy_document()
    changed = copy_document()
    metadata = changed["metadata"]
    assert isinstance(metadata, dict)
    metadata["version"] = 2

    assert _compile(original).plan_hash != _compile(changed).plan_hash


def test_compiler_rejects_unsupported_model() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, list)
    agent_node = nodes[0]
    assert isinstance(agent_node, dict)
    agent = agent_node["agent"]
    assert isinstance(agent, dict)
    agent["model"] = "unregistered/model"

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    assert {problem.code for problem in captured.value.issues} == {"agent.model_unsupported"}


def test_compiler_rejects_nested_open_output_object() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    spec["outputSchema"] = {
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            }
        },
        "required": ["result"],
        "additionalProperties": False,
    }

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    assert any(problem.code == "schema.object_not_closed" for problem in captured.value.issues)


def test_compiler_rejects_incomplete_strict_required() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    output_schema = spec["outputSchema"]
    assert isinstance(output_schema, dict)
    output_schema["required"] = []

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    assert any(problem.code == "schema.required_incomplete" for problem in captured.value.issues)


def test_compiler_rejects_dag_cycle() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    transitions = spec["transitions"]
    assert isinstance(transitions, list)
    transitions.append(
        {
            "id": "calculate-extract",
            "from": "calculate",
            "to": "extract",
            "on": "succeeded",
            "priority": 100,
        }
    )

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    assert any(problem.code == "dag.cycle" for problem in captured.value.issues)


def test_workspace_write_rejects_automatic_retries() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, list)
    agent_node = nodes[0]
    assert isinstance(agent_node, dict)
    agent = agent_node["agent"]
    assert isinstance(agent, dict)
    agent["access"] = "workspace_write"
    agent["retry"] = {"maximumAttempts": 2}

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    assert any(problem.code == "agent.write_retry_forbidden" for problem in captured.value.issues)


def _activity_node(node_id: str) -> JsonObject:
    return {
        "id": node_id,
        "type": "activity",
        "typeVersion": 1,
        "activity": {"name": "calculate", "version": 1},
    }


def test_compiler_rejects_workflow_above_node_limit() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    spec["nodes"] = [_activity_node(f"n{index}") for index in range(MAX_WORKFLOW_NODES + 1)]
    spec["transitions"] = []

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    problem = next(
        item for item in captured.value.issues if item.code == "workflow.node_limit_exceeded"
    )
    assert problem.context == {"limit": MAX_WORKFLOW_NODES, "actual": MAX_WORKFLOW_NODES + 1}


def test_compiler_rejects_workflow_above_transition_limit() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    spec["nodes"] = [_activity_node(f"n{index}") for index in range(100)]
    transitions: list[JsonValue] = []
    for source in range(100):
        for target in range(source + 1, 100):
            transitions.append(
                {
                    "id": f"e-{source}-{target}",
                    "from": f"n{source}",
                    "to": f"n{target}",
                    "priority": target,
                }
            )
            if len(transitions) > MAX_WORKFLOW_TRANSITIONS:
                break
        if len(transitions) > MAX_WORKFLOW_TRANSITIONS:
            break
    spec["transitions"] = transitions

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    problem = next(
        item for item in captured.value.issues if item.code == "workflow.transition_limit_exceeded"
    )
    assert problem.context == {
        "limit": MAX_WORKFLOW_TRANSITIONS,
        "actual": MAX_WORKFLOW_TRANSITIONS + 1,
    }


def _state_machine_document() -> JsonObject:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, list)
    nodes.append(_activity_node("fallback"))
    spec["flow"] = {
        "type": "state_machine",
        "initialNode": "extract",
        "maxTotalActivations": 100,
        "continueAsNewEvery": 10,
    }
    spec["maxConcurrency"] = 1
    spec["transitions"] = [
        {
            "id": "extract-calculate",
            "from": "extract",
            "to": "calculate",
            "when": "workflow.input.formula == '1+1'",
            "priority": 10,
        },
        {
            "id": "extract-fallback",
            "from": "extract",
            "to": "fallback",
            "priority": 100,
        },
        {
            "id": "calculate-extract",
            "from": "calculate",
            "to": "extract",
            "priority": 100,
        },
    ]
    return document


def test_state_machine_accepts_cycle_and_last_unconditional_fallback() -> None:
    plan = _compile(_state_machine_document())

    assert plan.mode == "state_machine"
    assert plan.initial_node_id == "extract"
    assert plan.max_total_activations == 100


def test_state_machine_rejects_unconditional_fallback_before_condition() -> None:
    document = _state_machine_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    transitions = spec["transitions"]
    assert isinstance(transitions, list)
    fallback = transitions[1]
    assert isinstance(fallback, dict)
    fallback["priority"] = 5

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    assert any(problem.code == "state.fallback_not_last" for problem in captured.value.issues)


def test_state_machine_rejects_multiple_unconditional_fallbacks() -> None:
    document = _state_machine_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    transitions = spec["transitions"]
    assert isinstance(transitions, list)
    transitions.append(
        {
            "id": "extract-second-fallback",
            "from": "extract",
            "to": "calculate",
            "priority": 200,
        }
    )

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    assert any(problem.code == "state.multiple_fallbacks" for problem in captured.value.issues)


def _quorum_document(required: int) -> JsonObject:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    transitions = spec["transitions"]
    assert isinstance(nodes, list)
    assert isinstance(transitions, list)
    nodes.append(
        {
            "id": "joined",
            "type": "join",
            "typeVersion": 1,
            "mode": "quorum",
            "required": required,
        }
    )
    transitions.extend(
        [
            {
                "id": "extract-joined",
                "from": "extract",
                "to": "joined",
                "priority": 200,
            },
            {
                "id": "calculate-joined",
                "from": "calculate",
                "to": "joined",
                "priority": 100,
            },
        ]
    )
    return document


def test_quorum_join_cannot_exceed_distinct_incoming_nodes() -> None:
    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(_quorum_document(required=3))

    problem = next(
        item for item in captured.value.issues if item.code == "join.quorum_exceeds_incoming"
    )
    assert problem.context == {"required": 3, "incoming": 2}


def test_quorum_join_accepts_available_incoming_nodes() -> None:
    plan = _compile(_quorum_document(required=2))

    joined = next(node for node in plan.nodes if node.id == "joined")
    assert joined.execution.kind == "join"
    assert joined.execution.required == 2


def test_compiler_rejects_duplicate_output_binding_names() -> None:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    outputs = spec["outputs"]
    assert isinstance(outputs, list)
    outputs.append({"name": "result", "expression": "nodes.extract.output.formula"})

    with pytest.raises(WorkflowCompilationError) as captured:
        _compile(document)

    assert any(
        problem.code == "binding.duplicate_name" and problem.path == "/spec/outputs/1/name"
        for problem in captured.value.issues
    )


def test_registered_activity_output_schema_must_be_strict() -> None:
    context = valid_context()
    invalid_activity = RegisteredActivity(
        name="calculate",
        version=1,
        output_schema={
            "type": "object",
            "properties": {
                "result": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                }
            },
            "required": ["result"],
            "additionalProperties": False,
        },
    )
    invalid_context = CompilationContext(
        catalog_revision=context.catalog_revision,
        provider_models=context.provider_models,
        workspace_ids=context.workspace_ids,
        activities=(invalid_activity,),
    )

    with pytest.raises(WorkflowCompilationError) as captured:
        compile_workflow(parse_json_workflow(json.dumps(copy_document())), invalid_context)

    assert any(
        problem.code == "schema.object_not_closed"
        and problem.path.startswith("/spec/nodes/1/activity/outputSchema")
        for problem in captured.value.issues
    )


def test_registered_activity_output_schema_is_frozen_into_plan() -> None:
    plan = _compile(copy_document())

    calculate = next(node for node in plan.nodes if node.id == "calculate")
    assert calculate.output_schema.canonical == json.dumps(
        closed_schema(result={"type": "integer"}),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
