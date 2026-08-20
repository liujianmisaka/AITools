from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from misaka_approval_capability import DecisionStore
from misaka_control_plane import (
    ControlPlaneConfig,
    ControlPlaneProfile,
    TemplateDAGRunner,
    TemplateNodeSubmission,
    TemplateRunResult,
)
from misaka_control_plane.template_registry import InstanceRecord, TemplateRecord
from misaka_coordinator_adapters import InvocationExecutionPlan
from misaka_coordinator_workflow import DAGCoordinator, DAGDefinition, DAGNode, WorkflowContext
from misaka_invocation_contracts import CompletionBoundary, InvocationRequest
from misaka_invocation_runtime import InvocationRuntime
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableJobStatus
from misaka_service_runtime import ServiceManager


@dataclass(frozen=True, slots=True)
class ControlPlaneWorkflowConfig:
    profile_id: str = "control-plane-workflow"
    profile_version: str = "1.0.0"
    transport_ids: tuple[str, ...] = ("fastapi", "in-process", "workflow")

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.profile_version.strip():
            raise ValueError("control-plane workflow profile identity must not be empty")
        if any(not item.strip() for item in self.transport_ids):
            raise ValueError("control-plane workflow transport ids must not be empty")
        if len(self.transport_ids) != len(set(self.transport_ids)):
            raise ValueError("control-plane workflow transport ids must be unique")


class ControlPlaneWorkflowProfile(ControlPlaneProfile):
    """Optional Control Plane composition with the DAG workflow adapter installed."""

    def __init__(
        self,
        runtime: InvocationRuntime,
        *,
        state_path: str | Path,
        config: ControlPlaneWorkflowConfig | None = None,
        shutdown_timeout_seconds: float = 15.0,
        provider_setup: Callable[[InvocationRuntime], Awaitable[None]] | None = None,
        decision_store: DecisionStore | None = None,
        service_manager: ServiceManager | None = None,
    ) -> None:
        settings = config or ControlPlaneWorkflowConfig()
        super().__init__(
            runtime,
            state_path=state_path,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
            provider_setup=provider_setup,
            dag_runner=create_dag_runner(runtime),
            decision_store=decision_store,
            service_manager=service_manager,
            config=ControlPlaneConfig(
                profile_id=settings.profile_id,
                profile_version=settings.profile_version,
                transport_ids=settings.transport_ids,
            ),
        )


def create_dag_runner(runtime: InvocationRuntime) -> TemplateDAGRunner:
    async def run(instance: InstanceRecord, template: TemplateRecord) -> TemplateRunResult:
        async def plan_for_node(
            context: WorkflowContext, node: TemplateNodeSubmission
        ) -> InvocationExecutionPlan:
            upstream = {
                node_id: result.output
                for node_id, result in context.outputs.items()
                if result.output is not None
            }
            input_payload = dict(node.input)
            input_payload.setdefault("instance_input", instance.input)
            input_payload["upstream_outputs"] = upstream
            request = InvocationRequest(
                invocation_id=f"instance:{instance.instance_id}:{node.node_id}",
                capability_id=node.capability_id,
                operation=node.operation,
                input=cast(JsonObject, input_payload),
                idempotency_key=f"instance:{instance.instance_id}:{node.node_id}",
                completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
                output_schema=node.output_schema,
                model=node.model,
                effort=node.effort,
                policy_context={"network_policy": node.network_policy},
            )
            return InvocationExecutionPlan(runtime, request, provider_id=node.provider_id)

        definition = DAGDefinition(
            tuple(
                DAGNode(
                    node_id=node.node_id,
                    depends_on=tuple(node.depends_on),
                    plan_factory=lambda context, node=node: plan_for_node(context, node),
                )
                for node in template.definition.nodes
            )
        )
        workflow_result = await DAGCoordinator().run(instance.instance_id, definition)
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
