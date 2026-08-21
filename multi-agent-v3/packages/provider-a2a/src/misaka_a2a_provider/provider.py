from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from misaka_a2a_capability import (
    A2AAgentCard,
    A2ASkill,
    RemoteTaskClient,
    TaskExecutionHandle,
    TaskRequest,
    TaskSnapshot,
    TaskStatus,
)
from misaka_invocation_contracts import (
    CapabilityDescriptor,
    CapabilityFeature,
    CapabilityOperation,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ProviderExecutionRef,
    ReconcileResult,
    ReconcileStatus,
)
from misaka_invocation_runtime import (
    ProviderContractError,
    ProviderHandle,
)

_SUPPORTED_REMOTE_FEATURES = frozenset(
    {
        CapabilityFeature.STRUCTURED_OUTPUT,
        CapabilityFeature.STREAMING,
        CapabilityFeature.CANCELLATION,
        CapabilityFeature.RESUME,
    }
)


@dataclass(frozen=True, slots=True)
class A2AProviderConfig:
    provider_id: str = "a2a"
    capability_id: str = "agent.invocation"
    operation: str = "invoke"
    context_prefix: str = "misaka-a2a-context"
    task_prefix: str = "misaka-a2a-task"
    remote_provider_id: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in {
            "provider_id": self.provider_id,
            "capability_id": self.capability_id,
            "operation": self.operation,
            "context_prefix": self.context_prefix,
            "task_prefix": self.task_prefix,
        }.items():
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.remote_provider_id is not None and not self.remote_provider_id.strip():
            raise ValueError("remote_provider_id must not be empty when provided")


class A2AInvocationProvider:
    """Expose a remote A2A Task endpoint through the Invocation Provider port."""

    def __init__(
        self,
        client: RemoteTaskClient,
        *,
        config: A2AProviderConfig | None = None,
    ) -> None:
        self.client = client
        self.config = config or A2AProviderConfig()
        self._descriptor: CapabilityDescriptor | None = None
        self._card: A2AAgentCard | None = None
        self._skill: A2ASkill | None = None
        self._describe_lock = asyncio.Lock()

    async def describe(self) -> CapabilityDescriptor:
        if self._descriptor is not None:
            return self._descriptor
        async with self._describe_lock:
            if self._descriptor is not None:
                return self._descriptor
            card = await self.client.describe()
            skill = _find_skill(card, self.config)
            features = (skill.features | card.features) & _SUPPORTED_REMOTE_FEATURES
            self._card = card
            self._skill = skill
            self._descriptor = CapabilityDescriptor(
                capability_id=self.config.capability_id,
                version=card.version,
                operations=(
                    CapabilityOperation(
                        name=self.config.operation,
                        input_schema=skill.input_schema,
                        output_schema=skill.output_schema,
                    ),
                ),
                features=features,
            )
            return self._descriptor

    async def start(self, request: InvocationRequest) -> ProviderHandle:
        descriptor = await self.describe()
        if request.capability_id != descriptor.capability_id:
            raise ProviderContractError(
                "a2a.capability_mismatch",
                "invocation capability does not match the configured A2A provider",
                reconciliation_required=False,
            )
        if request.operation != self.config.operation:
            raise ProviderContractError(
                "a2a.operation_mismatch",
                "invocation operation is not exposed by the configured A2A provider",
                reconciliation_required=False,
            )
        skill = self._skill
        if skill is None:
            raise ProviderContractError(
                "a2a.skill_unavailable",
                "A2A provider skill was not loaded",
                reconciliation_required=False,
            )
        _validate_required_fields(request, skill, descriptor.features)
        task_id = f"{self.config.task_prefix}:{request.invocation_id}"
        context_id = (
            request.session_ref.native_id
            if request.session_ref is not None
            else f"{self.config.context_prefix}:{request.invocation_id}"
        )
        task_request = TaskRequest(
            task_id=task_id,
            context_id=context_id,
            message_id=f"{task_id}:message:1",
            idempotency_key=request.idempotency_key,
            capability_id=request.capability_id,
            operation=request.operation,
            input=request.input,
            provider_id=self.config.remote_provider_id,
            model=request.model,
            effort=request.effort,
            session_ref=request.session_ref,
            required_features=request.required_features,
            output_schema=request.output_schema,
            policy_context=request.policy_context,
            metadata={"sourceInvocationId": request.invocation_id},
        )
        remote_handle = await self.client.submit(task_request)
        if remote_handle.task_id != task_id:
            raise ProviderContractError(
                "a2a.task_id_mismatch",
                "remote A2A endpoint returned a different task identity",
                reconciliation_required=True,
            )
        return _A2AProviderHandle(
            request,
            task_request,
            remote_handle,
            self.client,
        )

    async def reconcile_persisted(
        self,
        request: InvocationRequest,
        provider_execution: ProviderExecutionRef,
    ) -> ReconcileResult:
        task_id = provider_execution.provider_operation_id
        if task_id is None:
            return ReconcileResult(
                ReconcileStatus.UNREACHABLE,
                message="persisted A2A execution has no remote task id",
                error_code="a2a.task_id_missing",
            )
        try:
            snapshot = await self.client.get(task_id)
        except Exception as exc:
            return ReconcileResult(
                ReconcileStatus.UNREACHABLE,
                message=f"remote A2A task lookup failed: {exc}",
                error_code="a2a.task_lookup_failed",
                error_message=str(exc),
                provider_operation_id=task_id,
            )
        return _reconcile_snapshot(snapshot, request.invocation_id)

    async def close(self) -> None:
        await self.client.close()


class _A2AProviderHandle:
    def __init__(
        self,
        request: InvocationRequest,
        task_request: TaskRequest,
        remote_handle: TaskExecutionHandle,
        client: RemoteTaskClient,
    ) -> None:
        self._request = request
        self._task_request = task_request
        self._remote = remote_handle
        self._client = client
        self.provider_operation_id = task_request.task_id
        self.provider_session_id = task_request.context_id

    async def events(self) -> AsyncIterator[InvocationEvent]:
        last_sequence = 0
        async for event in self._remote.events(start_sequence=1):
            if event.task_id != self._task_request.task_id:
                raise ProviderContractError(
                    "a2a.remote_event_id_mismatch",
                    "remote A2A event belongs to another task",
                )
            if event.sequence <= last_sequence:
                continue
            if event.sequence != last_sequence + 1:
                raise ProviderContractError(
                    "a2a.remote_event_sequence_invalid",
                    "remote A2A event sequence is not contiguous",
                )
            last_sequence = event.sequence
            status = _invocation_status(event.status)
            if status in {
                InvocationStatus.SUCCEEDED,
                InvocationStatus.REJECTED,
                InvocationStatus.FAILED,
                InvocationStatus.CANCELLED,
                InvocationStatus.RECONCILIATION_REQUIRED,
            }:
                # InvocationRuntime owns terminal facts and writes them through
                # finalize(result); terminal observations must not be appended
                # as ordinary provider events.
                continue
            yield InvocationEvent(
                invocation_id=self._request.invocation_id,
                sequence=event.sequence,
                status=status,
                payload=event.payload,
                occurred_at=event.occurred_at,
            )

    async def wait(self) -> InvocationResult:
        result = await self._remote.wait()
        if result.task_id != self._task_request.task_id:
            raise ProviderContractError(
                "a2a.remote_result_id_mismatch",
                "remote A2A result belongs to another task",
            )
        return InvocationResult(
            invocation_id=self._request.invocation_id,
            status=_invocation_status(result.status),
            output=result.output,
            error_code=result.error_code,
            error_message=result.error_message,
            artifacts=result.artifacts,
        )

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        await self._remote.cancel(reason)

    async def reconcile(self) -> ReconcileResult:
        try:
            snapshot = await self._client.get(self._task_request.task_id)
        except Exception as exc:
            return ReconcileResult(
                ReconcileStatus.UNREACHABLE,
                message=f"remote A2A task lookup failed: {exc}",
                error_code="a2a.task_lookup_failed",
                error_message=str(exc),
                provider_operation_id=self.provider_operation_id,
                provider_session_id=self.provider_session_id,
            )
        return _reconcile_snapshot(snapshot, self._request.invocation_id)

    async def close(self) -> None:
        await self._remote.close()


def _find_skill(card: A2AAgentCard, config: A2AProviderConfig) -> A2ASkill:
    for skill in card.skills:
        if skill.capability_id == config.capability_id and skill.operation == config.operation:
            return skill
    raise ValueError(f"remote A2A card does not expose {config.capability_id}/{config.operation}")


def _validate_required_fields(
    request: InvocationRequest,
    skill: A2ASkill,
    features: frozenset[CapabilityFeature],
) -> None:
    required = skill.required_task_fields
    values: dict[str, object | None] = {
        "model": request.model,
        "effort": request.effort,
        "output_schema": request.output_schema,
        "session_ref": request.session_ref,
    }
    missing = sorted(field for field in required if values[field] is None)
    if missing:
        raise ProviderContractError(
            "a2a.task_field_missing",
            "remote A2A skill requires: " + ", ".join(missing),
            reconciliation_required=False,
        )
    if request.session_ref is not None and CapabilityFeature.RESUME not in features:
        raise ProviderContractError(
            "a2a.resume_unsupported",
            "remote A2A skill does not advertise session resume",
            reconciliation_required=False,
        )


def _invocation_status(status: TaskStatus) -> InvocationStatus:
    return {
        # The outer InvocationRuntime already owns REGISTERED/PREFLIGHTING;
        # a remote Task's submitted observation is therefore ordinary progress.
        TaskStatus.SUBMITTED: InvocationStatus.RUNNING,
        TaskStatus.WORKING: InvocationStatus.RUNNING,
        TaskStatus.INPUT_REQUIRED: InvocationStatus.RUNNING,
        TaskStatus.CANCELLING: InvocationStatus.STOPPING,
        TaskStatus.REJECTED: InvocationStatus.REJECTED,
        TaskStatus.COMPLETED: InvocationStatus.SUCCEEDED,
        TaskStatus.FAILED: InvocationStatus.FAILED,
        TaskStatus.CANCELLED: InvocationStatus.CANCELLED,
        TaskStatus.RECONCILIATION_REQUIRED: InvocationStatus.RECONCILIATION_REQUIRED,
    }[status]


def _reconcile_snapshot(snapshot: TaskSnapshot, invocation_id: str) -> ReconcileResult:
    if snapshot.result is None:
        return ReconcileResult(
            ReconcileStatus.RUNNING,
            message=f"remote A2A task is {snapshot.status.value}",
            provider_operation_id=snapshot.request.task_id,
            provider_session_id=snapshot.request.context_id,
            provider_turn_id=snapshot.request.message_id,
            attachable=False,
        )
    result = snapshot.result
    if result.task_id != snapshot.request.task_id:
        return ReconcileResult(
            ReconcileStatus.AMBIGUOUS,
            message="remote A2A task result identity is inconsistent",
            error_code="a2a.result_identity_invalid",
        )
    status = {
        TaskStatus.COMPLETED: ReconcileStatus.SUCCEEDED,
        TaskStatus.FAILED: ReconcileStatus.FAILED,
        TaskStatus.CANCELLED: ReconcileStatus.CANCELLED,
        TaskStatus.REJECTED: ReconcileStatus.FAILED,
        TaskStatus.RECONCILIATION_REQUIRED: ReconcileStatus.UNREACHABLE,
    }[result.status]
    return ReconcileResult(
        status,
        message=f"remote A2A task reconciled for {invocation_id}",
        provider_operation_id=snapshot.request.task_id,
        provider_session_id=snapshot.request.context_id,
        provider_turn_id=snapshot.request.message_id,
        output=result.output,
        error_code=result.error_code,
        error_message=result.error_message,
    )
