from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from misaka_a2a_capability import (
    A2AAgentCard,
    TaskCapabilityRejected,
    TaskEvent,
    TaskExecutionHandle,
    TaskHandler,
    TaskRequest,
    TaskResult,
    TaskStatus,
    task_request_fingerprint,
)
from misaka_delegation_capability import DelegationHandle, DelegationRuntimePort
from misaka_delegation_contracts import (
    DelegationMode,
    DelegationReport,
    DelegationRequest,
    DelegationStatus,
)
from misaka_interaction_contracts import (
    InteractionMessage,
    MessageCursor,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_invocation_contracts import InvocationStatus
from misaka_kernel_contracts import JsonObject


class DelegationTaskHandler(TaskHandler):
    """Maps A2A Task projections to the generic Delegation capability."""

    def __init__(
        self,
        runtime: DelegationRuntimePort,
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
        if request.session_ref is not None:
            raise TaskCapabilityRejected(
                "a2a.provider_session_unsupported",
                "A2A Delegation adapter does not accept provider-native session references",
            )
        delegation_id = delegation_id_for_task(self._card.agent_id, request)
        provider_id = self._provider_id or request.provider_id
        if (
            self._provider_id is not None
            and request.provider_id is not None
            and request.provider_id != self._provider_id
        ):
            raise TaskCapabilityRejected(
                "a2a.provider_mismatch",
                "A2A task requested provider "
                f"{request.provider_id!r}, but this node exposes {self._provider_id!r}",
            )
        principal = PrincipalRef(
            f"a2a:{request.context_id}",
            PrincipalKind.APPLICATION,
        )
        handle = await self._runtime.submit(
            DelegationRequest(
                delegation_id=delegation_id,
                idempotency_key=f"a2a:{self._card.agent_id}:{request.idempotency_key}",
                initiator=principal,
                controller=principal,
                scope=ScopeRef(f"a2a-context:{request.context_id}"),
                capability_id=request.capability_id,
                operation=request.operation,
                input=request.input,
                provider_id=provider_id,
                model=request.model,
                effort=request.effort,
                output_schema=request.output_schema,
                mode=DelegationMode.ONE_SHOT,
                channel_id=f"a2a-channel:{delegation_id}",
                required_features=frozenset(feature.value for feature in request.required_features),
                constraints=request.policy_context,
            )
        )
        snapshot = await handle.snapshot()
        invocation_id = snapshot.current_invocation_id
        activation_id = snapshot.current_activation_id
        if invocation_id is None and snapshot.report is not None:
            invocation_id = snapshot.report.source_invocation_id
            activation_id = snapshot.report.source_activation_id
        return DelegationTaskExecutionHandle(
            request.task_id,
            principal.principal_id,
            handle,
            channel_id=snapshot.ref.channel_id,
            stream_events=snapshot.report is None,
            invocation_id=invocation_id,
            activation_id=activation_id,
        )


class DelegationTaskExecutionHandle:
    def __init__(
        self,
        task_id: str,
        actor_id: str,
        handle: DelegationHandle,
        *,
        channel_id: str | None,
        stream_events: bool,
        invocation_id: str | None,
        activation_id: str | None,
    ) -> None:
        if stream_events and channel_id is None:
            raise TaskCapabilityRejected(
                "a2a.interaction_channel_missing",
                "A2A delegation did not create an interaction channel",
            )
        self._task_id = task_id
        self._actor_id = actor_id
        self._handle = handle
        self._channel_id = channel_id
        self._stream_events = stream_events
        self._invocation_id = invocation_id
        self._activation_id = activation_id

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def invocation_id(self) -> str | None:
        return self._invocation_id

    @property
    def delegation_id(self) -> str:
        return self._handle.delegation_id

    @property
    def activation_id(self) -> str | None:
        return self._activation_id

    async def events(
        self,
        *,
        start_sequence: int = 1,
    ) -> AsyncIterator[TaskEvent]:
        if not self._stream_events:
            return
        if self._channel_id is None:
            raise RuntimeError("streaming delegation handle has no interaction channel")
        cursor = MessageCursor(
            channel_id=self._channel_id,
            next_sequence=start_sequence,
        )
        async for message in self._handle.messages(cursor=cursor):
            yield _task_event_from_message(self._task_id, self.delegation_id, message)

    async def wait(self) -> TaskResult:
        return _task_result_from_report(
            self._task_id,
            self.delegation_id,
            await self._handle.wait(),
        )

    async def cancel(self, reason: str) -> None:
        await self._handle.cancel(self._actor_id, reason)

    async def close(self) -> None:
        snapshot = await self._handle.snapshot()
        if snapshot.report is None:
            await self._handle.cancel(self._actor_id, "A2A delegation handle closing")


def delegation_id_for_task(agent_id: str, request: TaskRequest) -> str:
    fingerprint = task_request_fingerprint(request)
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"misaka:a2a:delegation:{agent_id}:{fingerprint}")
    return f"del-{value}"


def _task_event_from_message(
    task_id: str,
    delegation_id: str,
    message: InteractionMessage,
) -> TaskEvent:
    raw_status = message.payload.get("status")
    status = _TASK_STATUS_BY_INVOCATION_STATUS_VALUE.get(
        raw_status if isinstance(raw_status, str) else "",
        TaskStatus.WORKING,
    )
    payload: JsonObject = {
        "delegation_id": delegation_id,
        "message_id": message.message_id,
        "message_type": message.message_type.value,
        "sender_id": message.sender.principal_id,
        "delivery_status": message.delivery_status.value,
        "message": message.payload,
    }
    if message.recipient is not None:
        payload["recipient_id"] = message.recipient.principal_id
    if message.correlation_id is not None:
        payload["correlation_id"] = message.correlation_id
    if message.causation_id is not None:
        payload["causation_id"] = message.causation_id
    if message.reply_to is not None:
        payload["reply_to"] = message.reply_to
    return TaskEvent(
        task_id=task_id,
        sequence=message.sequence,
        status=status,
        payload=payload,
        occurred_at=message.created_at,
    )


def _task_result_from_report(
    task_id: str,
    delegation_id: str,
    report: DelegationReport,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        invocation_id=report.source_invocation_id,
        delegation_id=delegation_id,
        activation_id=report.source_activation_id,
        status=_TASK_STATUS_BY_DELEGATION_STATUS[report.status],
        output=report.output,
        error_code=report.error_code,
        error_message=report.error_message,
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

_TASK_STATUS_BY_INVOCATION_STATUS_VALUE = {
    status.value: task_status for status, task_status in _TASK_STATUS_BY_INVOCATION_STATUS.items()
}

_TASK_STATUS_BY_DELEGATION_STATUS: dict[DelegationStatus, TaskStatus] = {
    DelegationStatus.PROPOSED: TaskStatus.SUBMITTED,
    DelegationStatus.ADMITTED: TaskStatus.SUBMITTED,
    DelegationStatus.PREPARING: TaskStatus.WORKING,
    DelegationStatus.ACTIVE: TaskStatus.WORKING,
    DelegationStatus.WAITING_INPUT: TaskStatus.INPUT_REQUIRED,
    DelegationStatus.REPORTING: TaskStatus.WORKING,
    DelegationStatus.COMPLETED: TaskStatus.COMPLETED,
    DelegationStatus.REJECTED: TaskStatus.REJECTED,
    DelegationStatus.FAILED: TaskStatus.FAILED,
    DelegationStatus.CANCELLED: TaskStatus.CANCELLED,
    DelegationStatus.RECONCILIATION_REQUIRED: TaskStatus.RECONCILIATION_REQUIRED,
    DelegationStatus.RECONCILING: TaskStatus.WORKING,
}
