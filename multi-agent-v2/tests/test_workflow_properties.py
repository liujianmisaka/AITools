from __future__ import annotations

import json
from typing import cast

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from multi_agent_v2.packages.domain.json_types import JsonObject, JsonScalar
from multi_agent_v2.packages.workflow_dsl import (
    WorkflowCompilationError,
    compile_workflow,
    parse_json_workflow,
    parse_yaml_workflow,
)
from multi_agent_v2.packages.workflow_dsl.canonical import canonical_json
from tests.workflow_samples import copy_document, valid_context


def _activity_node(node_id: str) -> JsonObject:
    return {
        "id": node_id,
        "type": "activity",
        "typeVersion": 1,
        "activity": {"name": "calculate", "version": 1},
    }


def _dag_document(node_count: int, edges: set[tuple[int, int]]) -> JsonObject:
    document = copy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    spec["nodes"] = [_activity_node(f"n{index}") for index in range(node_count)]
    spec["transitions"] = [
        {
            "id": f"e-{source}-{target}",
            "from": f"n{source}",
            "to": f"n{target}",
            "priority": target,
        }
        for source, target in sorted(edges)
        if source < node_count and target < node_count and source < target
    ]
    return document


@given(
    node_count=st.integers(min_value=1, max_value=8),
    candidate_edges=st.sets(
        st.tuples(st.integers(min_value=0, max_value=7), st.integers(min_value=0, max_value=7)),
        max_size=28,
    ),
)
def test_generated_forward_graphs_always_compile_as_dags(
    node_count: int,
    candidate_edges: set[tuple[int, int]],
) -> None:
    document = _dag_document(node_count, candidate_edges)

    plan = compile_workflow(
        parse_json_workflow(json.dumps(document)),
        valid_context(),
    )

    assert plan.mode == "dag"
    assert len(plan.nodes) == node_count


@given(node_count=st.integers(min_value=2, max_value=8))
def test_generated_back_edge_cycles_are_always_rejected(node_count: int) -> None:
    edges = {(index, index + 1) for index in range(node_count - 1)}
    document = _dag_document(node_count, edges)
    spec = document["spec"]
    assert isinstance(spec, dict)
    transitions = spec["transitions"]
    assert isinstance(transitions, list)
    transitions.append(
        {
            "id": "back-edge",
            "from": f"n{node_count - 1}",
            "to": "n0",
            "priority": 100,
        }
    )

    with pytest.raises(WorkflowCompilationError) as captured:
        compile_workflow(parse_json_workflow(json.dumps(document)), valid_context())

    assert any(problem.code == "dag.cycle" for problem in captured.value.issues)


_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.text(max_size=30),
)


@given(value=st.dictionaries(st.text(max_size=20), _JSON_SCALARS, max_size=20))
def test_canonical_json_ignores_object_insertion_order(value: dict[str, JsonScalar]) -> None:
    reversed_value = dict(reversed(tuple(value.items())))
    json_value = cast(JsonObject, value)
    reversed_json_value = cast(JsonObject, reversed_value)

    assert canonical_json(json_value) == canonical_json(reversed_json_value)
    assert canonical_json(json.loads(canonical_json(json_value))) == canonical_json(json_value)


def test_equivalent_json_and_yaml_documents_have_the_same_plan_hash() -> None:
    document = copy_document()

    json_plan = compile_workflow(
        parse_json_workflow(json.dumps(document)),
        valid_context(),
    )
    yaml_plan = compile_workflow(
        parse_yaml_workflow(yaml.safe_dump(document, sort_keys=False)),
        valid_context(),
    )

    assert json_plan.plan_hash == yaml_plan.plan_hash
