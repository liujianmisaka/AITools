from __future__ import annotations

import json
from typing import cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from temporalio.exceptions import ApplicationError

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_runtime.messages import (
    NodeActivityRequest,
    NodeActivityResult,
)


def successful_activity_result(
    request: NodeActivityRequest,
    output: JsonObject,
) -> NodeActivityResult:
    try:
        schema = cast(JsonObject, json.loads(request.output_schema.canonical))
        Draft202012Validator(schema).validate(output)  # pyright: ignore[reportUnknownMemberType]
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ApplicationError(
            "node output does not satisfy its compiled schema",
            type="NodeOutputContractViolation",
            non_retryable=True,
        ) from exc
    return NodeActivityResult(
        execution_id=request.execution_id,
        outcome="succeeded",
        output=output,
        output_schema_sha256=request.output_schema.sha256,
    )
