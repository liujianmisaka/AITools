from __future__ import annotations

import asyncio
import json

from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from multi_agent_v2.packages.domain.events import CloudEventEnvelope
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_dsl import compile_workflow, parse_json_workflow
from multi_agent_v2.packages.workflow_runtime.messages import (
    EventCommand,
    EventWaitCloseRequest,
    EventWaitSubscriptionRequest,
    ProjectionEventRequest,
    WorkflowRunInput,
)
from multi_agent_v2.packages.workflow_runtime.workflow import (
    ORCHESTRATION_TASK_QUEUE,
    WorkflowInstanceWorkflow,
)
from tests.workflow_samples import closed_schema, valid_context

_registered: list[EventWaitSubscriptionRequest] = []
_closed: list[EventWaitCloseRequest] = []


@activity.defn(name="event-wait.register.v1")
async def _register(request: EventWaitSubscriptionRequest) -> None:
    _registered.append(request)


@activity.defn(name="event-wait.close.v1")
async def _close(request: EventWaitCloseRequest) -> None:
    _closed.append(request)


@activity.defn(name="projection.publish.v1")
async def _project(_: ProjectionEventRequest) -> bool:
    return True


async def test_wait_event_is_durable_signalled_and_replayable() -> None:
    _registered.clear()
    _closed.clear()
    plan = compile_workflow(
        parse_json_workflow(json.dumps(_wait_event_document("P1D"))),
        valid_context(),
    )
    histories: list[WorkflowHistory] = []
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    ) as environment:
        async with (
            Worker(
                environment.client,
                task_queue="phase4-wait-event",
                workflows=[WorkflowInstanceWorkflow],
            ),
            Worker(
                environment.client,
                task_queue=ORCHESTRATION_TASK_QUEUE,
                activities=[_register, _close, _project],
            ),
        ):
            handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=plan,
                    workflow_input={"correlation": "build-42"},
                ),
                id="multi-agent-v2/instances/wait-event-test",
                task_queue="phase4-wait-event",
            )
            for _ in range(100):
                snapshot = await handle.query(WorkflowInstanceWorkflow.get_snapshot)
                if snapshot.nodes[0].status == "waiting_event":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("wait_event node did not become ready")
            await handle.signal(
                WorkflowInstanceWorkflow.deliver_event,
                EventCommand(
                    command_id="deliver-1",
                    node_id="wait",
                    activation=1,
                    event=CloudEventEnvelope(
                        id="event-1",
                        source="urn:test:build",
                        type="dev.misaka.build.completed.v1",
                        subject="build",
                        data={"value": 7},
                        extensions={"correlationkey": "build-42"},
                    ),
                ),
            )
            result = await handle.result()
            histories.append(await handle.fetch_history())

    assert result.status == "succeeded"
    assert result.output == {"value": 7}
    assert _registered[0].correlation_key == "build-42"
    assert _registered[0].output_schema.sha256 == plan.nodes[0].output_schema.sha256
    assert _closed == [
        EventWaitCloseRequest(
            instance_id="wait-event-test",
            node_id="wait",
            activation=1,
        )
    ]
    replayer = Replayer(
        workflows=[WorkflowInstanceWorkflow],
        data_converter=pydantic_data_converter,
    )
    for history in histories:
        await replayer.replay_workflow(history)


async def test_wait_event_timeout_closes_the_durable_subscription() -> None:
    _registered.clear()
    _closed.clear()
    plan = compile_workflow(
        parse_json_workflow(json.dumps(_wait_event_document("PT1S"))),
        valid_context(),
    )
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    ) as environment:
        async with (
            Worker(
                environment.client,
                task_queue="phase4-wait-timeout",
                workflows=[WorkflowInstanceWorkflow],
            ),
            Worker(
                environment.client,
                task_queue=ORCHESTRATION_TASK_QUEUE,
                activities=[_register, _close, _project],
            ),
        ):
            handle = await environment.client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=plan,
                    workflow_input={"correlation": "never"},
                ),
                id="multi-agent-v2/instances/wait-timeout-test",
                task_queue="phase4-wait-timeout",
            )
            result = await handle.result()

    assert result.status == "failed"
    assert result.error_code == "event.wait_timed_out"
    assert _closed[-1].instance_id == "wait-timeout-test"


def _wait_event_document(timeout: str) -> JsonObject:
    return {
        "apiVersion": "orchestration.misaka.dev/v1",
        "kind": "Workflow",
        "metadata": {"id": "wait-for-build", "version": 1, "name": "Wait for build"},
        "spec": {
            "flow": {"type": "dag"},
            "inputSchema": closed_schema(correlation={"type": "string"}),
            "outputSchema": closed_schema(value={"type": "integer"}),
            "failurePolicy": "continue_independent",
            "maxConcurrency": 1,
            "nodes": [
                {
                    "id": "wait",
                    "type": "wait_event",
                    "typeVersion": 1,
                    "eventType": "dev.misaka.build.completed.v1",
                    "sourcePattern": "urn:test:*",
                    "subjectPattern": "build",
                    "correlationExpression": "workflow.input.correlation",
                    "timeout": timeout,
                    "outputSchema": closed_schema(value={"type": "integer"}),
                }
            ],
            "transitions": [],
            "outputs": [{"name": "value", "expression": "nodes.wait.output.value"}],
        },
    }
