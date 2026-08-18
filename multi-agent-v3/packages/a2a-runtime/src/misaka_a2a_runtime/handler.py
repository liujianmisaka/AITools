from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from misaka_a2a_capability import (
    A2AAgentCard,
    TaskEvent,
    TaskExecutionHandle,
    TaskHandler,
    TaskRequest,
    TaskResult,
    TaskStatus,
    task_request_fingerprint,
)
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
)
from misaka_invocation_runtime import InvocationRuntime, RuntimeInvocationHandle


class InvocationTaskHandler(TaskHandler):
    """Maps external A2A tasks to internal invocations without owning transport."""

    def __init__(
        self,
        runtime: InvocationRuntime,
        card: A2AAgentCard,
        *,
        provider_id: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._card = card
        self._provider_id = provider_id

    async def describe(self) -> A2AAgentCard:
        return self._card

    async def submit(self, request: TaskRequest) -> TaskExecutionHandle:
        invocation_id = invocation_id_for_task(self._card.agent_id, request)
        invocation = InvocationRequest(
            invocation_id=invocation_id,
            capability_id=request.capability_id,
            operation=request.operation,
            input=request.input,
            idempotency_key=f"a2a:{self._card.agent_id}:{request.idempotency_key}",
            completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
            session_ref=request.session_ref,
            required_features=request.required_features,
            output_schema=request.output_schema,
            policy_context=request.policy_context,
            model=request.model,
            effort=request.effort,
        )
        handle = await self._runtime.submit(invocation, provider_id=self._provider_id)
        return InvocationTaskExecutionHandle(request.task_id, handle)


class InvocationTaskExecutionHandle:
    def __init__(self, task_id: str, handle: RuntimeInvocationHandle) -> None:
        self._task_id = task_id
        self._handle = handle

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def invocation_id(self) -> str:
        return self._handle.invocation_id

    async def events(
        self,
        *,
        start_sequence: int = 1,
    ) -> AsyncIterator[TaskEvent]:
        async for event in self._handle.events(start_sequence=start_sequence):
            yield _task_event(self._task_id, event)

    async def wait(self) -> TaskResult:
        return _task_result(self._task_id, await self._handle.wait())

    async def cancel(self, reason: str) -> None:
        await self._handle.cancel(reason)

    async def close(self) -> None:
        snapshot = await self._handle.snapshot()
        if snapshot.result is None:
            await self._handle.cancel("A2A task execution handle closing")


def invocation_id_for_task(agent_id: str, request: TaskRequest) -> str:
    fingerprint = task_request_fingerprint(request)
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"misaka:a2a:{agent_id}:{fingerprint}")
    return f"inv-{value}"


def _task_event(task_id: str, event: InvocationEvent) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        sequence=event.sequence,
        status=_TASK_STATUS_BY_INVOCATION_STATUS[event.status],
        payload={
            "invocation_id": event.invocation_id,
            "invocation_status": event.status.value,
            **event.payload,
        },
        occurred_at=event.occurred_at,
    )


def _task_result(task_id: str, result: InvocationResult) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        invocation_id=result.invocation_id,
        status=_TASK_STATUS_BY_INVOCATION_STATUS[result.status],
        output=result.output,
        artifacts=result.artifacts,
        error_code=result.error_code,
        error_message=result.error_message,
    )


_TASK_STATUS_BY_INVOCATION_STATUS: dict[InvocationStatus, TaskStatus] = {
    InvocationStatus.REGISTERED: TaskStatus.SUBMITTED,
    InvocationStatus.PREFLIGHTING: TaskStatus.WORKING,
    InvocationStatus.RESOURCE_ACQUIRING: TaskStatus.WORKING,
    InvocationStatus.PREPARED: TaskStatus.WORKING,
    InvocationStatus.STARTING: TaskStatus.WORKING,
    InvocationStatus.RUNNING: TaskStatus.WORKING,
    InvocationStatus.STOPPING: TaskStatus.CANCELLING,
    InvocationStatus.FINALIZING: TaskStatus.WORKING,
    InvocationStatus.SUCCEEDED: TaskStatus.COMPLETED,
    InvocationStatus.REJECTED: TaskStatus.REJECTED,
    InvocationStatus.FAILED: TaskStatus.FAILED,
    InvocationStatus.CANCELLED: TaskStatus.CANCELLED,
    InvocationStatus.RECONCILIATION_REQUIRED: TaskStatus.RECONCILIATION_REQUIRED,
}
