from __future__ import annotations

from typing import cast

from misaka_control_plane import TemplateDAGRunner, TemplateNodeSubmission, TemplateRunResult
from misaka_control_plane.template_registry import InstanceRecord, TemplateRecord
from misaka_coordinator_workflow import DAGCoordinator, DAGDefinition, DAGNode, WorkflowContext
from misaka_invocation_contracts import CompletionBoundary, InvocationRequest
from misaka_invocation_runtime import InvocationRuntime
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableJobStatus


def create_dag_runner(runtime: InvocationRuntime) -> TemplateDAGRunner:
    async def run(instance: InstanceRecord, template: TemplateRecord) -> TemplateRunResult:
        async def request_for_node(
            context: WorkflowContext, node: TemplateNodeSubmission
        ) -> InvocationRequest:
            upstream = {
                node_id: result.output
                for node_id, result in context.outputs.items()
                if result.output is not None
            }
            input_payload = dict(node.input)
            input_payload.setdefault("instance_input", instance.input)
            input_payload["upstream_outputs"] = upstream
            return InvocationRequest(
                invocation_id=f"instance:{instance.instance_id}:{node.node_id}",
                capability_id=node.capability_id,
                operation=node.operation,
                input=cast(JsonObject, input_payload),
                idempotency_key=f"instance:{instance.instance_id}:{node.node_id}",
                completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
                output_schema=node.output_schema,
                model=node.model,
                effort=node.effort,
                policy_context={"network": node.network_policy},
            )

        definition = DAGDefinition(
            tuple(
                DAGNode(
                    node_id=node.node_id,
                    depends_on=tuple(node.depends_on),
                    request_factory=lambda context, node=node: request_for_node(context, node),
                )
                for node in template.definition.nodes
            )
        )
        workflow_result = await DAGCoordinator(runtime).run(instance.instance_id, definition)
        status = {
            "succeeded": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "reconciliation_required": "reconciliation_required",
        }[workflow_result.status.value]
        result = cast(
            JsonObject,
            {
                node_id: {
                    "status": node_result.status.value,
                    "output": node_result.output,
                }
                for node_id, node_result in workflow_result.node_results.items()
            },
        )
        return TemplateRunResult(
            status=DurableJobStatus(status),
            result=result,
            error_message=workflow_result.error_message,
        )

    return run
