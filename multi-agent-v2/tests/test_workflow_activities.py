from __future__ import annotations

import json

import pytest
from temporalio.exceptions import ApplicationError

from multi_agent_v2.packages.workflow_dsl import compile_workflow, parse_json_workflow
from multi_agent_v2.packages.workflow_dsl.ir import AgentExecutionIr
from multi_agent_v2.packages.workflow_runtime.activities import successful_activity_result
from multi_agent_v2.packages.workflow_runtime.messages import NodeActivityRequest
from tests.workflow_samples import copy_document, valid_context


def test_activity_request_freezes_execution_and_output_contract() -> None:
    plan = compile_workflow(
        parse_json_workflow(json.dumps(copy_document())),
        valid_context(),
    )
    node = next(item for item in plan.nodes if item.id == "extract")
    assert isinstance(node.execution, AgentExecutionIr)
    request = NodeActivityRequest(
        workflow_instance_id="instance-1",
        plan_hash=plan.plan_hash,
        node_id=node.id,
        activation=1,
        execution_id="instance-1:extract:1",
        idempotency_key="instance-1:extract:1",
        resolved_inputs={"formula": "1 + 2"},
        provider_session_id=None,
        execution=node.execution,
        output_schema=node.output_schema,
    )

    result = successful_activity_result(request, {"formula": "1 + 2"})

    assert isinstance(request.execution, AgentExecutionIr)
    assert request.execution.provider == "codex"
    assert request.execution.model == "sensenova/deepseek-v4-flash"
    assert request.execution.effort == "high"
    assert result.execution_id == "instance-1:extract:1"
    assert result.output_schema_sha256 == node.output_schema.sha256
    with pytest.raises(ApplicationError):
        successful_activity_result(request, {"unexpected": True})
