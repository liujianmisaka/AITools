from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from multi_agent_v2.packages.control_plane.models import (
    ScheduleFireRequest,
    ScheduleTriggerInput,
)
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_runtime.workflow import ORCHESTRATION_TASK_QUEUE


@workflow.defn(name="ScheduleTriggerWorkflow")
class ScheduleTriggerWorkflow:
    @workflow.run
    async def run(self, trigger: ScheduleTriggerInput) -> JsonObject:
        return await workflow.execute_activity(
            "schedule.fire.v1",
            ScheduleFireRequest(
                schedule_id=trigger.schedule_id,
                schedule_revision=trigger.schedule_revision,
                occurrence_id=workflow.info().run_id,
                target=trigger.target,
            ),
            task_queue=ORCHESTRATION_TASK_QUEUE,
            result_type=dict,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=10,
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=30),
            ),
            summary=f"schedule-fire:{trigger.schedule_id}",
        )
