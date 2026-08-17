from __future__ import annotations

import asyncio
from pathlib import Path

from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_dsl import (
    CompilationContext,
    ProviderModel,
    RegisteredActivity,
    compile_workflow,
    parse_json_workflow,
)
from multi_agent_v2.packages.workflow_runtime.activities import successful_activity_result
from multi_agent_v2.packages.workflow_runtime.messages import (
    NodeActivityRequest,
    NodeActivityResult,
    ProjectionEventRequest,
    WorkflowRunInput,
)
from multi_agent_v2.packages.workflow_runtime.workflow import (
    AGENT_TASK_QUEUE,
    ORCHESTRATION_TASK_QUEUE,
    WorkflowInstanceWorkflow,
)
from tests.workflow_samples import closed_schema

_active_parallel_nodes = 0
_parallel_peak = 0
_executed_nodes: list[str] = []


async def _execute(request: NodeActivityRequest) -> NodeActivityResult:
    global _active_parallel_nodes, _parallel_peak
    _executed_nodes.append(request.node_id)
    if request.node_id in {"agent_check", "policy_check"}:
        _active_parallel_nodes += 1
        _parallel_peak = max(_parallel_peak, _active_parallel_nodes)
        await asyncio.sleep(0.05)
        _active_parallel_nodes -= 1
    outputs: dict[str, JsonObject] = {
        "prepare": {"prepared": True},
        "agent_check": {"analysis": "agent branch completed"},
        "policy_check": {"allowed": True},
        "ship": {"status": "shipped"},
        "review": {"status": "review_required"},
    }
    return successful_activity_result(request, outputs[request.node_id])


@activity.defn(name="agent.execute.v1")
async def execute_fake_agent(request: NodeActivityRequest) -> NodeActivityResult:
    return await _execute(request)


@activity.defn(name="registered-activity.execute.v1")
async def execute_fake_activity(request: NodeActivityRequest) -> NodeActivityResult:
    return await _execute(request)


@activity.defn(name="projection.publish.v1")
async def publish_projection(_: ProjectionEventRequest) -> bool:
    return True


async def test_multibranch_example_runs_both_terminal_routes_and_replays() -> None:
    global _active_parallel_nodes, _parallel_peak
    _active_parallel_nodes = 0
    _parallel_peak = 0
    _executed_nodes.clear()
    plan = _compile_example()
    histories: list[WorkflowHistory] = []
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    ) as environment:
        async with (
            Worker(
                environment.client,
                task_queue="phase7-multibranch",
                workflows=[WorkflowInstanceWorkflow],
            ),
            Worker(
                environment.client,
                task_queue=AGENT_TASK_QUEUE,
                activities=[execute_fake_agent],
            ),
            Worker(
                environment.client,
                task_queue=ORCHESTRATION_TASK_QUEUE,
                activities=[execute_fake_activity, publish_projection],
            ),
        ):
            shipped = await environment.client.execute_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(plan=plan, workflow_input={"release": True}),
                id="phase7-multibranch-ship",
                task_queue="phase7-multibranch",
            )
            ship_handle = environment.client.get_workflow_handle("phase7-multibranch-ship")
            histories.append(await ship_handle.fetch_history())
            shipped_nodes = set(_executed_nodes)
            _executed_nodes.clear()
            reviewed = await environment.client.execute_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(plan=plan, workflow_input={"release": False}),
                id="phase7-multibranch-review",
                task_queue="phase7-multibranch",
            )
            review_handle = environment.client.get_workflow_handle("phase7-multibranch-review")
            histories.append(await review_handle.fetch_history())
            reviewed_nodes = set(_executed_nodes)

    assert shipped.output == {"status": "shipped"}
    assert reviewed.output == {"status": "review_required"}
    assert _parallel_peak >= 2
    assert "ship" in shipped_nodes and "review" not in shipped_nodes
    assert "review" in reviewed_nodes and "ship" not in reviewed_nodes

    replayer = Replayer(
        workflows=[WorkflowInstanceWorkflow],
        data_converter=pydantic_data_converter,
    )
    for history in histories:
        await replayer.replay_workflow(history)


def _compile_example():
    example = Path(__file__).parents[1] / "examples" / "multi_branch" / "workflow.json"
    definition = parse_json_workflow(example.read_text(encoding="utf-8"))
    return compile_workflow(
        definition,
        CompilationContext(
            catalog_revision="phase7-example",
            provider_models=(
                ProviderModel(
                    provider="fake",
                    model="fake/model",
                    efforts=("high",),
                ),
            ),
            workspace_ids=("repo",),
            activities=(
                RegisteredActivity(
                    name="prepare",
                    version=1,
                    output_schema=closed_schema(prepared={"type": "boolean"}),
                ),
                RegisteredActivity(
                    name="policy_check",
                    version=1,
                    output_schema=closed_schema(allowed={"type": "boolean"}),
                ),
                RegisteredActivity(
                    name="ship",
                    version=1,
                    output_schema=closed_schema(status={"type": "string"}),
                ),
                RegisteredActivity(
                    name="review",
                    version=1,
                    output_schema=closed_schema(status={"type": "string"}),
                ),
            ),
        ),
    )
