from __future__ import annotations

from typing import Any

from misaka_invocation_contracts import InvocationResult
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from misaka_coordinator_temporal.contracts import TemporalInvocationInput, TemporalResultPayload
from misaka_coordinator_temporal.workflow import TemporalInvocationWorkflow


class TemporalExecutionHandle:
    def __init__(self, handle: WorkflowHandle[Any, TemporalResultPayload]) -> None:
        self._handle = handle

    @property
    def workflow_id(self) -> str:
        return self._handle.id

    @property
    def run_id(self) -> str | None:
        return self._handle.result_run_id

    async def wait(self) -> InvocationResult:
        return (await self._handle.result()).to_result()

    async def cancel(self) -> None:
        await self._handle.cancel()


class TemporalCoordinator:
    """Starts one durable Temporal workflow without a second application fact source."""

    def __init__(self, client: Client, *, task_queue: str) -> None:
        if not task_queue.strip():
            raise ValueError("task_queue must not be empty")
        self._client = client
        self._task_queue = task_queue

    async def start(
        self,
        workflow_id: str,
        input: TemporalInvocationInput,
    ) -> TemporalExecutionHandle:
        if not workflow_id.strip():
            raise ValueError("workflow_id must not be empty")
        handle = await self._client.start_workflow(
            TemporalInvocationWorkflow.run,
            input,
            id=workflow_id,
            task_queue=self._task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        return TemporalExecutionHandle(handle)

    def get_handle(self, workflow_id: str) -> TemporalExecutionHandle:
        if not workflow_id.strip():
            raise ValueError("workflow_id must not be empty")
        handle = self._client.get_workflow_handle(
            workflow_id,
            result_type=TemporalResultPayload,
        )
        return TemporalExecutionHandle(handle)
