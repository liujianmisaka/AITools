from __future__ import annotations

import json

import pytest

from multi_agent_v2.packages.workflow_dsl import (
    WorkflowCompilationError,
    parse_json_workflow,
    parse_yaml_workflow,
)
from tests.workflow_samples import valid_workflow_document


def test_json_and_yaml_parse_to_the_same_definition() -> None:
    document = valid_workflow_document()

    from_json = parse_json_workflow(json.dumps(document))
    from_yaml = parse_yaml_workflow(json.dumps(document))

    assert from_json == from_yaml


def test_json_rejects_duplicate_keys() -> None:
    with pytest.raises(WorkflowCompilationError) as captured:
        parse_json_workflow('{"kind":"Workflow","kind":"Workflow"}')

    assert captured.value.issues[0].code == "document.invalid_json"


@pytest.mark.parametrize(
    "document",
    [
        "root: &shared {value: 1}\ncopy: *shared\n",
        "base: &base {value: 1}\ncopy:\n  <<: *base\n",
        "key: 1\nkey: 2\n",
        "value: !unsafe tag\n",
    ],
)
def test_yaml_rejects_alias_merge_duplicate_and_custom_tag(document: str) -> None:
    with pytest.raises(WorkflowCompilationError) as captured:
        parse_yaml_workflow(document)

    assert captured.value.issues[0].code == "document.invalid_yaml"


def test_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(WorkflowCompilationError) as captured:
        parse_json_workflow('{"value":NaN}')

    assert captured.value.issues[0].code == "document.invalid_json"


def test_dsl_rejects_unknown_fields() -> None:
    document = valid_workflow_document()
    metadata = document["metadata"]
    assert isinstance(metadata, dict)
    metadata["unexpected"] = True

    with pytest.raises(WorkflowCompilationError) as captured:
        parse_json_workflow(json.dumps(document))

    assert captured.value.issues[0].code == "dsl.invalid"
    assert captured.value.issues[0].path == "/metadata/unexpected"


def test_agent_selection_has_no_default_model() -> None:
    document = valid_workflow_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, list)
    first = nodes[0]
    assert isinstance(first, dict)
    agent = first["agent"]
    assert isinstance(agent, dict)
    del agent["model"]

    with pytest.raises(WorkflowCompilationError) as captured:
        parse_json_workflow(json.dumps(document))

    assert any(problem.path == "/spec/nodes/0/agent/model" for problem in captured.value.issues)
