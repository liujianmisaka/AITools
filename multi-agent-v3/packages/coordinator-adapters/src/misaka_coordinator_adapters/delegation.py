from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from misaka_coordinator_runtime import (
    ExecutionEvent,
    ExecutionHandle,
    ExecutionResult,
    ExecutionStatus,
    ReconciliationResult,
    ReconciliationState,
)
from misaka_delegation_capability import DelegationHandle, DelegationRuntimePort
from misaka_delegation_contracts import (
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
    delegation_request_fingerprint,
)
from misaka_interaction_contracts import (
    InteractionMessage,
    MessageCursor,
    MessageType,
)
from misaka_kernel_contracts import JsonObject


@dataclass(frozen=True, slots=True)
class DelegationExecutionPlan:
    runtime: DelegationRuntimePort
    request: DelegationRequest

    @property
    def execution_id(self) -> str:
        return self.request.delegation_id

    @property
    def fingerprint(self) -> str:
        return delegation_request_fingerprint(self.request)

    async def start(self, *, attempt: int = 1) -> DelegationExecutionHandle:
        if attempt < 1:
            raise ValueError("attempt must be at least one")
        if attempt != 1:
            raise ValueError(
                "DelegationExecutionPlan does not support an automatic retry attempt; "
                "create a new delegation identity"
            )
        handle = await self.runtime.submit(self.request)
        snapshot = await handle.snapshot()
        return DelegationExecutionHandle(
            handle,
            actor_id=self.request.controller.principal_id,
            activation_id=_activation_id(snapshot),
        )


class DelegationExecutionHandle(ExecutionHandle):
    """Expose one Delegation activation through the provider-neutral Execution Port."""

    def __init__(
        self,
        handle: DelegationHandle,
        *,
        actor_id: str,
        activation_id: str,
    ) -> None:
        if not actor_id.strip():
            raise ValueError("actor_id must not be empty")
        if not activation_id.strip():
            raise ValueError("activation_id must not be empty")
        self._handle = handle
        self._actor_id = actor_id
        self._activation_id = activation_id

    @property
    def execution_id(self) -> str:
        return self._handle.delegation_id

    @property
    def activation_id(self) -> str:
        return self._activation_id

    def events(self, *, start_sequence: int = 1) -> AsyncIterator[ExecutionEvent]:
        return self._events(start_sequence=start_sequence)

    async def _events(self, *, start_sequence: int) -> AsyncIterator[ExecutionEvent]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        snapshot = await self._handle.snapshot()
        channel_id = snapshot.ref.channel_id
        if channel_id is None:
            result = await self.wait()
            yield ExecutionEvent(
                execution_id=result.execution_id,
                sequence=start_sequence,
                status=result.status,
                payload=_result_payload(result),
            )
            return

        last_sequence = start_sequence - 1
        terminal_seen = False
        async for message in self._handle.messages(
            cursor=MessageCursor(channel_id, start_sequence)
        ):
            event = _execution_event(message, self.execution_id)
            last_sequence = max(last_sequence, event.sequence)
            terminal_seen = terminal_seen or event.status in _TERMINAL_STATUSES
            yield event
            if event.status in _TERMINAL_STATUSES:
                break
        if not terminal_seen:
            result = await self.wait()
            yield ExecutionEvent(
                execution_id=result.execution_id,
                sequence=max(last_sequence + 1, start_sequence),
                status=result.status,
                payload=_result_payload(result),
            )

    async def wait(self) -> ExecutionResult:
        return _execution_result(await self._handle.wait(), self.activation_id)

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        await self._handle.cancel(self._actor_id, reason)

    async def reconcile(self) -> ReconciliationResult:
        snapshot = await self._handle.snapshot()
        return _reconciliation_result(snapshot)


_TERMINAL_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.RECONCILIATION_REQUIRED,
    }
)


def _execution_event(message: InteractionMessage, execution_id: str) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id=execution_id,
        sequence=message.sequence,
        status=_message_status(message),
        payload=_message_payload(message),
        occurred_at=message.created_at,
    )


def _message_status(message: InteractionMessage) -> ExecutionStatus:
    if message.message_type is MessageType.QUESTION:
        return ExecutionStatus.WAITING_INPUT
    if message.message_type is not MessageType.RESULT:
        return ExecutionStatus.RUNNING
    raw_status = message.payload.get("status")
    if not isinstance(raw_status, str):
        return ExecutionStatus.RUNNING
    return {
        "succeeded": ExecutionStatus.SUCCEEDED,
        "completed": ExecutionStatus.SUCCEEDED,
        "rejected": ExecutionStatus.FAILED,
        "failed": ExecutionStatus.FAILED,
        "cancelled": ExecutionStatus.CANCELLED,
        "reconciliation_required": ExecutionStatus.RECONCILIATION_REQUIRED,
    }.get(raw_status, ExecutionStatus.RUNNING)


def _message_payload(message: InteractionMessage) -> JsonObject:
    payload: JsonObject = {
        "message_id": message.message_id,
        "channel_id": message.channel_id,
        "message_type": message.message_type.value,
        "delivery_status": message.delivery_status.value,
        "payload": message.payload,
        "sender": {
            "principal_id": message.sender.principal_id,
            "kind": message.sender.kind.value,
        },
    }
    if message.correlation_id is not None:
        payload["correlation_id"] = message.correlation_id
    if message.causation_id is not None:
        payload["causation_id"] = message.causation_id
    if message.reply_to is not None:
        payload["reply_to"] = message.reply_to
    return payload


def _execution_result(report: DelegationReport, activation_id: str) -> ExecutionResult:
    status = {
        DelegationStatus.COMPLETED: ExecutionStatus.SUCCEEDED,
        DelegationStatus.REJECTED: ExecutionStatus.FAILED,
        DelegationStatus.FAILED: ExecutionStatus.FAILED,
        DelegationStatus.CANCELLED: ExecutionStatus.CANCELLED,
        DelegationStatus.RECONCILIATION_REQUIRED: ExecutionStatus.RECONCILIATION_REQUIRED,
    }[report.status]
    metadata: JsonObject = {}
    if report.source_invocation_id is not None:
        metadata["source_invocation_id"] = report.source_invocation_id
    if report.source_activation_id is not None:
        metadata["source_activation_id"] = report.source_activation_id
    if report.artifact_ids:
        metadata["artifact_ids"] = list(report.artifact_ids)
    return ExecutionResult(
        execution_id=report.delegation_id,
        activation_id=report.source_activation_id or activation_id,
        status=status,
        output=report.output,
        error_code=report.error_code,
        error_message=report.error_message,
        metadata=metadata,
    )


def _activation_id(snapshot: DelegationSnapshot) -> str:
    if snapshot.current_activation_id is not None:
        return snapshot.current_activation_id
    if snapshot.report is not None and snapshot.report.source_activation_id is not None:
        return snapshot.report.source_activation_id
    return f"{snapshot.ref.delegation_id}:activation:{max(snapshot.activation_count, 1)}"


def _result_payload(result: ExecutionResult) -> JsonObject:
    payload: JsonObject = {"status": result.status.value}
    if result.output is not None:
        payload["output"] = result.output
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    if result.error_message is not None:
        payload["error_message"] = result.error_message
    return payload


def _reconciliation_result(snapshot: DelegationSnapshot) -> ReconciliationResult:
    if snapshot.report is not None:
        report = snapshot.report
        execution_result = _execution_result(
            report,
            snapshot.current_activation_id or report.source_activation_id or "reconciled",
        )
        state = {
            ExecutionStatus.SUCCEEDED: ReconciliationState.SUCCEEDED,
            ExecutionStatus.FAILED: ReconciliationState.FAILED,
            ExecutionStatus.CANCELLED: ReconciliationState.CANCELLED,
            ExecutionStatus.RECONCILIATION_REQUIRED: ReconciliationState.UNREACHABLE,
        }[execution_result.status]
        return ReconciliationResult(
            state=state,
            message=f"Delegation status: {snapshot.status.value}",
            output=execution_result.output,
            error_code=execution_result.error_code,
            error_message=execution_result.error_message,
        )

    state = {
        DelegationStatus.PROPOSED: ReconciliationState.NOT_STARTED,
        DelegationStatus.ADMITTED: ReconciliationState.NOT_STARTED,
        DelegationStatus.PREPARING: ReconciliationState.NOT_STARTED,
        DelegationStatus.ACTIVE: ReconciliationState.RUNNING,
        DelegationStatus.WAITING_INPUT: ReconciliationState.RUNNING,
        DelegationStatus.RECONCILING: ReconciliationState.RUNNING,
        DelegationStatus.RECONCILIATION_REQUIRED: ReconciliationState.UNREACHABLE,
    }.get(snapshot.status, ReconciliationState.UNREACHABLE)
    return ReconciliationResult(
        state=state,
        message=f"Delegation status: {snapshot.status.value}",
    )
