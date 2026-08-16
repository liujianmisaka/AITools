from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from multi_agent_v2.packages.control_plane.models import GitRefTarget
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_runtime.workflow import ORCHESTRATION_TASK_QUEUE


@workflow.defn(name="GitConnectorWorkflow")
class GitConnectorWorkflow:
    @workflow.run
    async def run(self, target: GitRefTarget) -> JsonObject:
        return await workflow.execute_activity(
            "git.poll.v1",
            target,
            task_queue=ORCHESTRATION_TASK_QUEUE,
            result_type=dict,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                initial_interval=timedelta(seconds=2),
                maximum_interval=timedelta(seconds=30),
            ),
            summary=f"git-poll:{target.connector_id}",
        )
