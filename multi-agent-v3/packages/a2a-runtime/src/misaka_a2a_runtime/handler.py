from __future__ import annotations

import asyncio
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
)
from misaka_delegation_capability import (
    DelegationHandle,
    DelegationNotFound,
    DelegationRuntimePort,
)
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationMode,
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
)
from misaka_interaction_contracts import (
    InteractionMessage,
    MessageCursor,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_invocation_contracts import CapabilityFeature, InvocationStatus
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
        self._submission_lock = asyncio.Lock()

    async def describe(self) -> A2AAgentCard:
        return self._card

    async def submit(self, request: TaskRequest) -> TaskExecutionHandle:
        if request.session_ref is not None and request.session_ref.native_id != request.context_id:
            raise TaskCapabilityRejected(
                "a2a.context_session_mismatch",
                "A2A session reference must identify the task context",
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
        async with self._submission_lock:
            try:
                existing = await self._runtime.snapshot(delegation_id)
            except DelegationNotFound:
                existing = None
            if existing is None:
                start_channel_sequence = 1
                handle = await self._runtime.submit(
                    DelegationRequest(
                        delegation_id=delegation_id,
                        idempotency_key=(
                            f"a2a:{self._card.agent_id}:{request.context_id}:delegation"
                        ),
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
                        mode=DelegationMode.CONTINUABLE,
                        session_id=(f"a2a-session:{self._card.agent_id}:{request.context_id}"),
                        channel_id=f"a2a-channel:{delegation_id}",
                        required_features=_delegation_features(request),
                        constraints=request.policy_context,
                    )
                )
            else:
                _validate_context_configuration(existing, request, provider_id)
                if existing.current_invocation_id is not None:
                    raise TaskCapabilityRejected(
                        "a2a.context_busy",
                        "A2A context already has a live activation",
                    )
                if not existing.report_history:
                    raise TaskCapabilityRejected(
                        "a2a.context_activation_missing",
                        "A2A context has no completed activation to continue",
                    )
                previous_activation_id = existing.report_history[-1].source_activation_id
                if previous_activation_id is None:
                    raise TaskCapabilityRejected(
                        "a2a.context_activation_missing",
                        "A2A context has no stable activation identity",
                    )
                messages = await self._runtime.read_messages(delegation_id)
                start_channel_sequence = messages[-1].sequence + 1 if messages else 1
                handle = await self._runtime.continue_request(
                    ContinuationRequest(
                        request_id=request.task_id,
                        delegation_id=delegation_id,
                        operation=ContinuationOperation.FOLLOW_UP,
                        actor=principal,
                        idempotency_key=request.idempotency_key,
                        session_id=existing.ref.session_id,
                        message_id=request.message_id,
                        expected_activation_id=previous_activation_id,
                        input=request.input,
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
            start_channel_sequence=start_channel_sequence,
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
        start_channel_sequence: int,
        invocation_id: str | None,
        activation_id: str | None,
    ) -> None:
        if channel_id is None:
            raise TaskCapabilityRejected(
                "a2a.interaction_channel_missing",
                "A2A delegation did not create an interaction channel",
            )
        self._task_id = task_id
        self._actor_id = actor_id
        self._handle = handle
        self._channel_id = channel_id
        self._start_channel_sequence = start_channel_sequence
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
        cursor = MessageCursor(
            channel_id=self._channel_id,
            next_sequence=self._start_channel_sequence,
        )
        task_sequence = 0
        async for message in self._handle.messages(cursor=cursor):
            if message.payload.get("activation_id") != self._activation_id:
                continue
            task_sequence += 1
            event = _task_event_from_message(
                self._task_id,
                self.delegation_id,
                message,
                sequence=task_sequence,
            )
            if task_sequence >= start_sequence:
                yield event
            if event.status in _TERMINAL_PROJECTED_TASK_STATUSES:
                return

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
    value = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"misaka:a2a:delegation:{agent_id}:{request.context_id}",
    )
    return f"del-{value}"


def _task_event_from_message(
    task_id: str,
    delegation_id: str,
    message: InteractionMessage,
    *,
    sequence: int,
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
        "channel_sequence": message.sequence,
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
        sequence=sequence,
        status=status,
        payload=payload,
        occurred_at=message.created_at,
    )


def _delegation_features(request: TaskRequest) -> frozenset[str]:
    return frozenset(
        feature.value
        for feature in request.required_features
        if feature is not CapabilityFeature.RESUME
    )


def _validate_context_configuration(
    snapshot: DelegationSnapshot,
    request: TaskRequest,
    provider_id: str | None,
) -> None:
    configured = snapshot.request
    expected = (
        request.capability_id,
        request.operation,
        provider_id,
        request.model,
        request.effort,
        _delegation_features(request),
        request.output_schema,
        request.policy_context,
    )
    actual = (
        configured.capability_id,
        configured.operation,
        configured.provider_id,
        configured.model,
        configured.effort,
        configured.required_features,
        configured.output_schema,
        configured.constraints,
    )
    if actual != expected:
        raise TaskCapabilityRejected(
            "a2a.context_configuration_mismatch",
            "A2A context cannot change its fixed delegation configuration",
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

_TERMINAL_PROJECTED_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.REJECTED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.RECONCILIATION_REQUIRED,
    }
)

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
