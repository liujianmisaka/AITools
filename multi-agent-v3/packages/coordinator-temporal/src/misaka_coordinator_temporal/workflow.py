from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from misaka_coordinator_temporal.contracts import TemporalInvocationInput, TemporalResultPayload

TEMPORAL_INVOCATION_WORKFLOW = "misaka.invocation.v1"
TEMPORAL_INVOCATION_ACTIVITY = "misaka.execute-invocation.v1"


@workflow.defn(name=TEMPORAL_INVOCATION_WORKFLOW)
class TemporalInvocationWorkflow:
    @workflow.run
    async def run(self, input: TemporalInvocationInput) -> TemporalResultPayload:
        return await workflow.execute_activity(
            TEMPORAL_INVOCATION_ACTIVITY,
            input,
            result_type=TemporalResultPayload,
            start_to_close_timeout=timedelta(seconds=input.start_to_close_timeout_seconds),
            heartbeat_timeout=timedelta(seconds=input.heartbeat_timeout_seconds),
            retry_policy=RetryPolicy(maximum_attempts=input.maximum_attempts),
            activity_id=f"{input.invocation_id}:execute",
        )
