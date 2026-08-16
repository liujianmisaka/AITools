from __future__ import annotations

import asyncio
import os

import pytest
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker


@workflow.defn
class WaitingWorkflow:
    def __init__(self) -> None:
        self._released = False

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self._released)
        return "released"

    @workflow.signal
    def release(self) -> None:
        self._released = True


@pytest.mark.capacity
@pytest.mark.timeout(180)
@pytest.mark.skipif(
    os.getenv("MULTI_AGENT_V2_RUN_CAPACITY_TESTS") != "1",
    reason="1000-workflow capacity test requires explicit opt-in",
)
async def test_one_thousand_waiting_workflows_use_a_bounded_worker_pool() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue="phase6-capacity",
            workflows=[WaitingWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            max_concurrent_workflow_tasks=32,
        ):
            with environment.auto_time_skipping_disabled():
                handles = await asyncio.gather(
                    *(
                        environment.client.start_workflow(
                            WaitingWorkflow.run,
                            id=f"phase6-wait-{index}",
                            task_queue="phase6-capacity",
                        )
                        for index in range(1000)
                    )
                )
                await asyncio.gather(
                    *(handle.signal(WaitingWorkflow.release) for handle in handles)
                )
                results = await asyncio.gather(*(handle.result() for handle in handles))

    assert results == ["released"] * 1000
