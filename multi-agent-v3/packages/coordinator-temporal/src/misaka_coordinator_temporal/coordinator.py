from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, replace
from typing import Any

from misaka_coordinator_runtime import (
    ExecutionEvent,
    ExecutionHandle,
    ExecutionResult,
    ExecutionStatus,
    ReconciliationResult,
    ReconciliationState,
)
from misaka_invocation_contracts import InvocationResult, InvocationStatus
from misaka_kernel_contracts import JsonObject
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import CancelledError as TemporalCancelledError

from misaka_coordinator_temporal.contracts import TemporalInvocationInput, TemporalResultPayload
from misaka_coordinator_temporal.workflow import TemporalInvocationWorkflow


@dataclass(frozen=True, slots=True)
class TemporalExecutionPlan:
    client: Client
    task_queue: str
    input: TemporalInvocationInput
    workflow_id: str | None = None

    @property
    def execution_id(self) -> str:
        return self.input.invocation_id

    @property
    def fingerprint(self) -> str:
        input_payload = asdict(self.input)
        # Activity delivery attempts are execution metadata, not logical request identity.
        input_payload.pop("attempt", None)
        payload = {
            "workflow_id": self.workflow_id,
            "task_queue": self.task_queue,
            "input": input_payload,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def start(self, *, attempt: int = 1) -> TemporalExecutionHandle:
        if attempt < 1:
            raise ValueError("attempt must be at least one")
        if not self.task_queue.strip():
            raise ValueError("task_queue must not be empty")
        if self.workflow_id is not None and not self.workflow_id.strip():
            raise ValueError("workflow_id must not be empty when provided")
        input_value = self.input
        workflow_id = self.workflow_id or input_value.invocation_id
        if attempt > 1:
            input_value = replace(
                self.input,
                invocation_id=f"{self.input.invocation_id}:attempt:{attempt}",
                idempotency_key=f"{self.input.idempotency_key}:attempt:{attempt}",
                attempt=attempt,
            )
            workflow_id = f"{workflow_id}:attempt:{attempt}"
        handle = await self.client.start_workflow(
            TemporalInvocationWorkflow.run,
            input_value,
            id=workflow_id,
            task_queue=self.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        return TemporalExecutionHandle(
            handle,
            execution_id=input_value.invocation_id,
            attempt=attempt,
        )


class TemporalExecutionHandle(ExecutionHandle):
    def __init__(
        self,
        handle: WorkflowHandle[Any, TemporalResultPayload],
        *,
        execution_id: str,
        attempt: int = 1,
    ) -> None:
        self._handle = handle
        self._execution_id = execution_id
        self._attempt = attempt

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def activation_id(self) -> str:
        return f"{self._execution_id}:activation:{self._attempt}"

    @property
    def workflow_id(self) -> str:
        return self._handle.id

    @property
    def run_id(self) -> str | None:
        return self._handle.result_run_id

    def events(self, *, start_sequence: int = 1) -> AsyncIterator[ExecutionEvent]:
        return self._events(start_sequence=start_sequence)

    async def _events(self, *, start_sequence: int) -> AsyncIterator[ExecutionEvent]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        if start_sequence > 1:
            return
        result = await self.wait()
        yield ExecutionEvent(
            execution_id=result.execution_id,
            sequence=1,
            status=result.status,
            payload=_result_payload(result),
        )

    async def wait(self) -> ExecutionResult:
        try:
            payload = await self._handle.result()
        except WorkflowFailureError as exc:
            return _temporal_failure_result(
                self.execution_id,
                self.activation_id,
                exc,
            )
        except Exception as exc:
            return ExecutionResult(
                execution_id=self.execution_id,
                activation_id=self.activation_id,
                status=ExecutionStatus.RECONCILIATION_REQUIRED,
                error_code="temporal.result_unavailable",
                error_message=str(exc) or exc.__class__.__name__,
            )
        return _execution_result(payload.to_result(), self.activation_id)

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        await self._handle.cancel()

    async def reconcile(self) -> ReconciliationResult:
        try:
            description = await self._handle.describe()
        except Exception as exc:
            return ReconciliationResult(
                state=ReconciliationState.UNREACHABLE,
                message=f"Temporal workflow status unavailable: {exc}",
            )
        raw_status = _status_name(description.status)
        state = {
            "RUNNING": ReconciliationState.RUNNING,
            "CONTINUED_AS_NEW": ReconciliationState.RUNNING,
            "COMPLETED": ReconciliationState.SUCCEEDED,
            "FAILED": ReconciliationState.FAILED,
            "CANCELED": ReconciliationState.CANCELLED,
            "CANCELLED": ReconciliationState.CANCELLED,
            "TERMINATED": ReconciliationState.FAILED,
            "TIMED_OUT": ReconciliationState.FAILED,
        }.get(raw_status, ReconciliationState.UNREACHABLE)
        return ReconciliationResult(state=state, message=f"Temporal workflow status: {raw_status}")


class TemporalCoordinator:
    """Durable Coordinator adapter; Temporal owns orchestration, not domain facts."""

    def __init__(self, client: Client, *, task_queue: str) -> None:
        if not task_queue.strip():
            raise ValueError("task_queue must not be empty")
        self._client = client
        self._task_queue = task_queue

    async def submit(self, plan: TemporalExecutionPlan) -> TemporalExecutionHandle:
        if plan.client is not self._client:
            raise ValueError("Temporal execution plan belongs to another client")
        if plan.task_queue != self._task_queue:
            raise ValueError("Temporal execution plan belongs to another task queue")
        return await plan.start()

    def get_handle(
        self,
        workflow_id: str,
        *,
        execution_id: str | None = None,
        attempt: int = 1,
    ) -> TemporalExecutionHandle:
        if not workflow_id.strip():
            raise ValueError("workflow_id must not be empty")
        if attempt < 1:
            raise ValueError("attempt must be at least one")
        handle = self._client.get_workflow_handle(
            workflow_id,
            result_type=TemporalResultPayload,
        )
        resolved_execution_id = execution_id or workflow_id
        return TemporalExecutionHandle(
            handle,
            execution_id=resolved_execution_id,
            attempt=attempt,
        )


def _execution_result(result: InvocationResult, activation_id: str) -> ExecutionResult:
    status = {
        InvocationStatus.SUCCEEDED: ExecutionStatus.SUCCEEDED,
        InvocationStatus.REJECTED: ExecutionStatus.FAILED,
        InvocationStatus.FAILED: ExecutionStatus.FAILED,
        InvocationStatus.CANCELLED: ExecutionStatus.CANCELLED,
        InvocationStatus.RECONCILIATION_REQUIRED: ExecutionStatus.RECONCILIATION_REQUIRED,
    }.get(result.status)
    if status is None:
        raise ValueError(f"unsupported invocation result status: {result.status.value}")
    return ExecutionResult(
        execution_id=result.invocation_id,
        activation_id=activation_id,
        status=status,
        output=result.output,
        error_code=result.error_code,
        error_message=result.error_message,
    )


def _status_name(status: Any) -> str:
    name = getattr(status, "name", None)
    if isinstance(name, str):
        return name.upper()
    return str(status).rsplit(".", maxsplit=1)[-1].upper()


def _temporal_failure_result(
    execution_id: str,
    activation_id: str,
    error: WorkflowFailureError,
) -> ExecutionResult:
    cause = error.cause
    if isinstance(cause, TemporalCancelledError):
        status = ExecutionStatus.CANCELLED
        error_code = "temporal.cancelled"
    else:
        status = ExecutionStatus.RECONCILIATION_REQUIRED
        error_code = "temporal.workflow_failure"
    return ExecutionResult(
        execution_id=execution_id,
        activation_id=activation_id,
        status=status,
        error_code=error_code,
        error_message=str(cause) or str(error) or error.__class__.__name__,
    )


def _result_payload(result: ExecutionResult) -> JsonObject:
    payload: JsonObject = {"status": result.status.value}
    if result.output is not None:
        payload["output"] = result.output
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    if result.error_message is not None:
        payload["error_message"] = result.error_message
    return payload
