from __future__ import annotations

import jmespath
from jmespath.exceptions import JMESPathError

from multi_agent_v2.packages.domain.json_types import JsonValue
from multi_agent_v2.packages.workflow_dsl.errors import CompilationIssue, issue


def validate_expression(expression: str, *, path: str) -> CompilationIssue | None:
    try:
        jmespath.compile(expression)
    except JMESPathError as exc:
        return issue("expression.invalid", path, str(exc))
    return None


def evaluate_expression(expression: str, context: JsonValue) -> JsonValue:
    return jmespath.search(expression, context)


def evaluate_condition(expression: str, context: JsonValue) -> bool:
    result = evaluate_expression(expression, context)
    if type(result) is not bool:
        raise ValueError("JMESPath condition must return a boolean")
    return result
