from __future__ import annotations

import asyncio
import json

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowHistory, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_dsl import compile_workflow, parse_json_workflow
from multi_agent_v2.packages.workflow_runtime.activities import successful_activity_result
from multi_agent_v2.packages.workflow_runtime.messages import (
    ApprovalCommand,
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
from tests.workflow_samples import closed_schema, copy_document, valid_context


async def _execute_fake_node(request: NodeActivityRequest) -> NodeActivityResult:
    output: JsonObject
    if request.node_id == "right":
        await asyncio.sleep(0.05)
    if request.node_id == "slow":
        await asyncio.sleep(0.2)
    if request.node_id == "extract":
        output = {"formula": request.resolved_inputs["formula"]}
    else:
        output = {"result": 3}
    return successful_activity_result(request, output)


@activity.defn(name="agent.execute.v1")
async def execute_fake_agent(request: NodeActivityRequest) -> NodeActivityResult:
    return await _execute_fake_node(request)


@activity.defn(name="registered-activity.execute.v1")
async def execute_fake_registered_activity(
    request: NodeActivityRequest,
) -> NodeActivityResult:
    return await _execute_fake_node(request)


@activity.defn(name="projection.publish.v1")
async def publish_fake_projection(_: ProjectionEventRequest) -> bool:
    return True


async def _wait_for_pending_approval(handle: object) -> None:
    for _ in range(100):
        pending = await handle.query("approvals.pending.v1")  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
        if pending == ["approve"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("approval node did not reach its waiting state")


@pytest.mark.asyncio
async def test_temporal_phase2_runtime_contracts() -> None:
    dag_plan = _compile(copy_document())
    parallel_plan = _compile(_parallel_document())
    approval_plan = _compile(_approval_document())
    timer_plan = _compile(_timer_document())
    continue_plan = _compile(_continue_as_new_document())
    timeout_plan = _compile(_timeout_state_machine_document())
    invalid_expression_plan = _compile(_invalid_expression_document())
    histories: list[WorkflowHistory] = []
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    ) as environment:
        async with (
            Worker(
                environment.client,
                task_queue="phase2-dag",
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
                activities=[execute_fake_registered_activity, publish_fake_projection],
            ),
        ):
            handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(plan=dag_plan, workflow_input={"formula": "1 + 2"}),
                id="phase2-dag-instance",
                task_queue="phase2-dag",
            )
            dag_result = await handle.result()
            histories.append(await handle.fetch_history())

            parallel_handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=parallel_plan,
                    workflow_input={"formula": "parallel"},
                ),
                id="phase2-parallel-instance",
                task_queue="phase2-dag",
            )
            parallel_result = await parallel_handle.result()
            histories.append(await parallel_handle.fetch_history())

            approval_handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=approval_plan,
                    workflow_input={"formula": "approval"},
                ),
                id="phase2-approval-instance",
                task_queue="phase2-dag",
            )
            await _wait_for_pending_approval(approval_handle)
            command = ApprovalCommand(
                command_id="approve-command",
                node_id="approve",
                activation=1,
                decision="approved",
                operator_label="tester",
                reason=None,
            )
            accepted = await approval_handle.execute_update(  # pyright: ignore[reportUnknownMemberType]
                WorkflowInstanceWorkflow.decide_approval,
                command,
                id=command.command_id,
            )
            temporal_retry = await approval_handle.execute_update(  # pyright: ignore[reportUnknownMemberType]
                WorkflowInstanceWorkflow.decide_approval,
                command,
                id=command.command_id,
            )
            defensive_duplicate = await approval_handle.execute_update(  # pyright: ignore[reportUnknownMemberType]
                WorkflowInstanceWorkflow.decide_approval,
                command,
                id="approval-defensive-duplicate",
            )
            assert accepted.accepted is True
            assert temporal_retry.accepted is True
            assert defensive_duplicate.accepted is False
            with pytest.raises(WorkflowUpdateFailedError):
                await approval_handle.execute_update(  # pyright: ignore[reportUnknownMemberType]
                    WorkflowInstanceWorkflow.decide_approval,
                    command.model_copy(
                        update={"command_id": "reject-command", "decision": "rejected"}
                    ),
                    id="reject-command",
                )
            approval_result = await approval_handle.result()
            histories.append(await approval_handle.fetch_history())

            approval_timeout_handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=approval_plan,
                    workflow_input={"formula": "approval-timeout"},
                ),
                id="phase2-approval-timeout-instance",
                task_queue="phase2-dag",
            )
            approval_timeout_result = await approval_timeout_handle.result()
            histories.append(await approval_timeout_handle.fetch_history())

            approval_signal_handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=approval_plan,
                    workflow_input={"formula": "approval-signal"},
                ),
                id="phase2-approval-signal-instance",
                task_queue="phase2-dag",
            )
            await _wait_for_pending_approval(approval_signal_handle)
            await approval_signal_handle.signal(
                WorkflowInstanceWorkflow.submit_approval,
                ApprovalCommand(
                    command_id="approval-signal-command",
                    node_id="approve",
                    activation=1,
                    decision="approved",
                ),
            )
            approval_signal_result = await approval_signal_handle.result()
            histories.append(await approval_signal_handle.fetch_history())

            timer_handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(plan=timer_plan, workflow_input={"formula": "timer"}),
                id="phase2-timer-instance",
                task_queue="phase2-dag",
            )
            await timer_handle.cancel(reason="test cancellation")
            with pytest.raises(WorkflowFailureError):
                await timer_handle.result()
            histories.append(await timer_handle.fetch_history())

            timeout_handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=timeout_plan,
                    workflow_input={"formula": "slow"},
                ),
                id="phase2-activity-timeout-instance",
                task_queue="phase2-dag",
            )
            timeout_result = await timeout_handle.result()
            histories.append(await timeout_handle.fetch_history())

            invalid_expression_handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=invalid_expression_plan,
                    workflow_input={"formula": "not-a-boolean"},
                ),
                id="phase2-invalid-expression-instance",
                task_queue="phase2-dag",
            )
            with pytest.raises(WorkflowFailureError):
                await invalid_expression_handle.result()
            histories.append(await invalid_expression_handle.fetch_history())

            continue_handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=continue_plan,
                    workflow_input={"formula": "continue"},
                ),
                id="phase2-continue-instance",
                task_queue="phase2-dag",
            )
            continue_result = await continue_handle.result()
            initial_continue_handle = environment.client.get_workflow_handle(
                "phase2-continue-instance",
                run_id=continue_handle.first_execution_run_id,
            )
            continue_history = await initial_continue_handle.fetch_history()
            histories.append(continue_history)
            final_continue_handle = environment.client.get_workflow_handle(
                "phase2-continue-instance",
                run_id=continue_handle.result_run_id,
            )
            histories.append(await final_continue_handle.fetch_history())

    assert dag_result.status == "succeeded"
    assert dag_result.output == {"result": 3}
    assert parallel_result.status == "succeeded"
    assert parallel_result.output == {"completed": ["left"]}
    assert approval_result.status == "succeeded"
    assert approval_result.output == {"fired": True}
    assert approval_timeout_result.status == "failed"
    assert approval_timeout_result.error_code == "approval.timed_out"
    assert approval_signal_result.status == "succeeded"
    assert approval_signal_result.output == {"fired": True}
    assert timeout_result.status == "succeeded"
    assert timeout_result.output == {"recovered": True}
    assert continue_result.status == "succeeded"
    assert continue_result.output == {"result": 1}
    assert any(
        event.HasField("workflow_execution_continued_as_new_event_attributes")
        for event in continue_history.events
    )
    replayer = Replayer(
        workflows=[WorkflowInstanceWorkflow],
        data_converter=pydantic_data_converter,
    )
    for history in histories:
        await replayer.replay_workflow(history)


def _compile(document: JsonObject):  # type annotation inferred from compiler boundary
    return compile_workflow(parse_json_workflow(json.dumps(document)), valid_context())


def _timer_document() -> JsonObject:
    document = copy_document()
    metadata = document["metadata"]
    spec = document["spec"]
    assert isinstance(metadata, dict)
    assert isinstance(spec, dict)
    metadata["id"] = "timer"
    spec["outputSchema"] = closed_schema(fired={"type": "boolean"})
    spec["nodes"] = [{"id": "wait", "type": "timer", "delay": "P30D"}]
    spec["transitions"] = []
    spec["outputs"] = [{"name": "fired", "expression": "nodes.wait.output.fired"}]
    return document


def _parallel_document() -> JsonObject:
    document = copy_document()
    metadata = document["metadata"]
    spec = document["spec"]
    assert isinstance(metadata, dict)
    assert isinstance(spec, dict)
    metadata["id"] = "parallel"
    spec["outputSchema"] = closed_schema(completed={"type": "array", "items": {"type": "string"}})
    spec["nodes"] = [
        {
            "id": "left",
            "type": "activity",
            "inputs": [{"name": "formula", "expression": "workflow.input.formula"}],
            "activity": {"name": "calculate", "version": 1},
        },
        {
            "id": "right",
            "type": "activity",
            "inputs": [{"name": "formula", "expression": "workflow.input.formula"}],
            "activity": {"name": "calculate", "version": 1},
        },
        {"id": "join", "type": "join", "mode": "any"},
    ]
    spec["transitions"] = [
        {
            "id": "left-join",
            "from": "left",
            "to": "join",
            "on": "succeeded",
            "priority": 100,
        },
        {
            "id": "right-join",
            "from": "right",
            "to": "join",
            "on": "succeeded",
            "priority": 100,
        },
    ]
    spec["outputs"] = [{"name": "completed", "expression": "nodes.join.output.completed"}]
    return document


def _approval_document() -> JsonObject:
    document = _timer_document()
    metadata = document["metadata"]
    spec = document["spec"]
    assert isinstance(metadata, dict)
    assert isinstance(spec, dict)
    metadata["id"] = "approval"
    spec["nodes"] = [
        {"id": "approve", "type": "approval", "label": "Approve", "timeout": "P7D"},
        {"id": "wait", "type": "timer", "delay": "P1D"},
    ]
    spec["transitions"] = [
        {
            "id": "approve-wait",
            "from": "approve",
            "to": "wait",
            "on": "succeeded",
            "priority": 100,
        }
    ]
    return document


def _continue_as_new_document() -> JsonObject:
    document = copy_document()
    metadata = document["metadata"]
    spec = document["spec"]
    assert isinstance(metadata, dict)
    assert isinstance(spec, dict)
    metadata["id"] = "continue-loop"
    spec["flow"] = {
        "type": "state_machine",
        "initialNode": "choose",
        "maxTotalActivations": 12,
        "continueAsNewEvery": 10,
    }
    spec["outputSchema"] = closed_schema(result={"type": "integer"})
    spec["nodes"] = [
        {
            "id": "choose",
            "type": "decision",
            "expression": "workflow.input.formula != null",
        },
        {
            "id": "loop",
            "type": "decision",
            "expression": "workflow.input.formula != null",
        },
    ]
    spec["transitions"] = [
        {
            "id": "choose-loop",
            "from": "choose",
            "to": "loop",
            "on": "succeeded",
            "priority": 10,
        },
        {
            "id": "loop-choose",
            "from": "loop",
            "to": "choose",
            "on": "succeeded",
            "when": "nodes.loop.activation <= `5`",
            "priority": 10,
        },
    ]
    spec["outputs"] = [{"name": "result", "expression": "`1`"}]
    return document


def _timeout_state_machine_document() -> JsonObject:
    document = copy_document()
    metadata = document["metadata"]
    spec = document["spec"]
    assert isinstance(metadata, dict)
    assert isinstance(spec, dict)
    metadata["id"] = "activity-timeout"
    spec["flow"] = {
        "type": "state_machine",
        "initialNode": "slow",
        "maxTotalActivations": 3,
    }
    spec["outputSchema"] = closed_schema(recovered={"type": "boolean"})
    spec["nodes"] = [
        {
            "id": "slow",
            "type": "activity",
            "inputs": [{"name": "formula", "expression": "workflow.input.formula"}],
            "activity": {
                "name": "calculate",
                "version": 1,
                "timeout": "PT0.01S",
                "retry": {"maximumAttempts": 1},
            },
        },
        {
            "id": "recovered",
            "type": "decision",
            "expression": "`true`",
        },
    ]
    spec["transitions"] = [
        {
            "id": "slow-recovered",
            "from": "slow",
            "to": "recovered",
            "on": "timed_out",
            "priority": 100,
        }
    ]
    spec["outputs"] = [{"name": "recovered", "expression": "nodes.recovered.output.result"}]
    return document


def _invalid_expression_document() -> JsonObject:
    document = copy_document()
    metadata = document["metadata"]
    spec = document["spec"]
    assert isinstance(metadata, dict)
    assert isinstance(spec, dict)
    metadata["id"] = "invalid-expression"
    spec["outputSchema"] = closed_schema(result={"type": "boolean"})
    spec["nodes"] = [{"id": "decide", "type": "decision", "expression": "workflow.input.formula"}]
    spec["transitions"] = []
    spec["outputs"] = [{"name": "result", "expression": "nodes.decide.output.result"}]
    return document
