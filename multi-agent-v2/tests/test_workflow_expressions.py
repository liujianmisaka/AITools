from __future__ import annotations

import json

import pytest
from jmespath.exceptions import JMESPathError

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_dsl.expressions import (
    evaluate_condition,
    evaluate_expression,
    validate_expression,
)


def test_jmespath_reads_only_the_supplied_context() -> None:
    context: JsonObject = {
        "workflow": {"input": {"formula": "1+1"}},
        "nodes": {"extract": {"output": {"formula": "2+2"}}},
    }

    assert evaluate_expression("nodes.extract.output.formula", context) == "2+2"
    assert evaluate_condition("workflow.input.formula == '1+1'", context) is True


def test_invalid_jmespath_is_reported_at_the_supplied_path() -> None:
    problem = validate_expression("nodes.[", path="/spec/nodes/0/expression")

    assert problem is not None
    assert problem.code == "expression.invalid"
    assert problem.path == "/spec/nodes/0/expression"


@pytest.mark.parametrize("expression", ["`1`", "'true'", "missing.path"])
def test_condition_requires_an_actual_boolean(expression: str) -> None:
    with pytest.raises(ValueError, match="must return a boolean"):
        evaluate_condition(expression, {})


def test_python_looking_text_is_data_and_unknown_functions_are_not_executed() -> None:
    payload = "__import__('os').system('should-not-run')"

    assert evaluate_expression(f"`{json.dumps(payload)}`", {}) == payload
    with pytest.raises(JMESPathError):
        evaluate_expression("__import__('os')", {})
