from __future__ import annotations

from copy import deepcopy

from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue
from multi_agent_v2.packages.workflow_dsl import (
    CompilationContext,
    ProviderModel,
    RegisteredActivity,
)


def closed_schema(**properties: JsonValue) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def valid_workflow_document() -> JsonObject:
    return {
        "apiVersion": "orchestration.misaka.dev/v1",
        "kind": "Workflow",
        "metadata": {"id": "addition", "version": 1, "name": "Addition"},
        "spec": {
            "flow": {"type": "dag"},
            "inputSchema": closed_schema(formula={"type": "string"}),
            "outputSchema": closed_schema(result={"type": "integer"}),
            "failurePolicy": "continue_independent",
            "maxConcurrency": 4,
            "nodes": [
                {
                    "id": "extract",
                    "type": "agent",
                    "typeVersion": 1,
                    "inputs": [{"name": "formula", "expression": "workflow.input.formula"}],
                    "outputSchema": closed_schema(formula={"type": "string"}),
                    "agent": {
                        "provider": "codex",
                        "model": "sensenova/deepseek-v4-flash",
                        "effort": "high",
                        "workspaceId": "repo",
                        "access": "read_only",
                        "sessionMode": "new",
                        "instruction": "Read the formula.",
                        "timeout": "PT5M",
                        "retry": {"maximumAttempts": 1},
                    },
                },
                {
                    "id": "calculate",
                    "type": "activity",
                    "typeVersion": 1,
                    "inputs": [{"name": "formula", "expression": "nodes.extract.output.formula"}],
                    "activity": {
                        "name": "calculate",
                        "version": 1,
                        "timeout": "PT1M",
                    },
                },
            ],
            "transitions": [
                {
                    "id": "extract-calculate",
                    "from": "extract",
                    "to": "calculate",
                    "on": "succeeded",
                    "priority": 100,
                }
            ],
            "outputs": [{"name": "result", "expression": "nodes.calculate.output.result"}],
        },
    }


def valid_context() -> CompilationContext:
    return CompilationContext(
        catalog_revision="catalog-1",
        provider_models=(
            ProviderModel(
                provider="codex",
                model="sensenova/deepseek-v4-flash",
                efforts=("high", "ultra"),
            ),
        ),
        workspace_ids=("repo",),
        activities=(
            RegisteredActivity(
                name="calculate",
                version=1,
                output_schema=closed_schema(result={"type": "integer"}),
            ),
        ),
    )


def copy_document() -> JsonObject:
    return deepcopy(valid_workflow_document())
