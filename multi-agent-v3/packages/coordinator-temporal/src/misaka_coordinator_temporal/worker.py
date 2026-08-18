from __future__ import annotations

from misaka_invocation_runtime import InvocationRuntime
from temporalio.client import Client
from temporalio.worker import Worker

from misaka_coordinator_temporal.activity_runner import InvocationActivityRunner
from misaka_coordinator_temporal.workflow import TemporalInvocationWorkflow


def build_temporal_worker(
    client: Client,
    runtime: InvocationRuntime,
    *,
    task_queue: str,
) -> Worker:
    if not task_queue.strip():
        raise ValueError("task_queue must not be empty")
    runner = InvocationActivityRunner(runtime)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[TemporalInvocationWorkflow],
        activities=[runner.execute],
    )
