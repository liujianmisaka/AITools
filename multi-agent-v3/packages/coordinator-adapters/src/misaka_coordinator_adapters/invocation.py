from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

from misaka_coordinator_runtime import (
    ExecutionEvent,
    ExecutionHandle,
    ExecutionResult,
    ExecutionStatus,
    ReconciliationResult,
    ReconciliationState,
)
from misaka_invocation_contracts import (
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ReconcileResult,
    ReconcileStatus,
    request_fingerprint,
)
from misaka_invocation_runtime import InvocationRuntime, RuntimeInvocationHandle


@dataclass(frozen=True, slots=True)
class InvocationExecutionPlan:
    runtime: InvocationRuntime
    request: InvocationRequest
    provider_id: str | None = None

    @property
    def execution_id(self) -> str:
        return self.request.invocation_id

    @property
    def fingerprint(self) -> str:
        payload = {
            "request": request_fingerprint(self.request),
            "provider_id": self.provider_id,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def start(self, *, attempt: int = 1) -> ExecutionHandle:
        if attempt < 1:
            raise ValueError("attempt must be at least one")
        request = self.request
        if attempt > 1:
            request = replace(
                request,
                invocation_id=f"{self.request.invocation_id}:attempt:{attempt}",
                idempotency_key=f"{self.request.idempotency_key}:attempt:{attempt}",
                attempt=attempt,
            )
        handle = await self.runtime.submit(request, provider_id=self.provider_id)
        return InvocationExecutionHandle(handle, attempt=attempt)


class InvocationExecutionHandle:
    def __init__(self, handle: RuntimeInvocationHandle, *, attempt: int) -> None:
        self._handle = handle
        self._attempt = attempt

    @property
    def execution_id(self) -> str:
        return self._handle.invocation_id

    @property
    def activation_id(self) -> str:
        return f"{self.execution_id}:activation:{self._attempt}"

    def events(self, *, start_sequence: int = 1) -> AsyncIterator[ExecutionEvent]:
        return self._events(start_sequence=start_sequence)

    async def _events(self, *, start_sequence: int) -> AsyncIterator[ExecutionEvent]:
        async for event in self._handle.events(start_sequence=start_sequence):
            yield _execution_event(event)

    async def wait(self) -> ExecutionResult:
        return _execution_result(await self._handle.wait(), self.activation_id)

    async def cancel(self, reason: str) -> None:
        await self._handle.cancel(reason)

    async def reconcile(self) -> ReconciliationResult:
        return _reconciliation_result(await self._handle.reconcile())


def _execution_event(event: InvocationEvent) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id=event.invocation_id,
        sequence=event.sequence,
        status=_execution_status(event.status),
        payload=event.payload,
        occurred_at=event.occurred_at,
    )


def _execution_result(result: InvocationResult, activation_id: str) -> ExecutionResult:
    return ExecutionResult(
        execution_id=result.invocation_id,
        activation_id=activation_id,
        status=_execution_status(result.status),
        output=result.output,
        error_code=result.error_code,
        error_message=result.error_message,
    )


def _execution_status(status: InvocationStatus) -> ExecutionStatus:
    return {
        InvocationStatus.SUCCEEDED: ExecutionStatus.SUCCEEDED,
        InvocationStatus.REJECTED: ExecutionStatus.FAILED,
        InvocationStatus.FAILED: ExecutionStatus.FAILED,
        InvocationStatus.CANCELLED: ExecutionStatus.CANCELLED,
        InvocationStatus.RECONCILIATION_REQUIRED: ExecutionStatus.RECONCILIATION_REQUIRED,
        InvocationStatus.REGISTERED: ExecutionStatus.SUBMITTED,
        InvocationStatus.PREFLIGHTING: ExecutionStatus.RUNNING,
        InvocationStatus.RESOURCE_ACQUIRING: ExecutionStatus.RUNNING,
        InvocationStatus.PREPARED: ExecutionStatus.RUNNING,
        InvocationStatus.STARTING: ExecutionStatus.RUNNING,
        InvocationStatus.RUNNING: ExecutionStatus.RUNNING,
        InvocationStatus.STOPPING: ExecutionStatus.RUNNING,
        InvocationStatus.FINALIZING: ExecutionStatus.RUNNING,
    }[status]


def _reconciliation_result(result: ReconcileResult) -> ReconciliationResult:
    state = {
        ReconcileStatus.NOT_STARTED: ReconciliationState.NOT_STARTED,
        ReconcileStatus.RUNNING: ReconciliationState.RUNNING,
        ReconcileStatus.SUCCEEDED: ReconciliationState.SUCCEEDED,
        ReconcileStatus.FAILED: ReconciliationState.FAILED,
        ReconcileStatus.CANCELLED: ReconciliationState.CANCELLED,
        ReconcileStatus.NOT_FOUND: ReconciliationState.NOT_FOUND,
        ReconcileStatus.AMBIGUOUS: ReconciliationState.AMBIGUOUS,
        ReconcileStatus.UNREACHABLE: ReconciliationState.UNREACHABLE,
    }[result.status]
    return ReconciliationResult(
        state=state,
        message=result.message,
        output=result.output,
        error_code=result.error_code,
        error_message=result.error_message,
        last_sequence=result.last_sequence,
    )
