from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

from misaka_delegation_capability import (
    DelegationCapabilityRejected,
    DelegationHandle,
    DelegationRuntimePort,
    DelegationStateError,
    DelegationStore,
    DelegationUnauthorized,
)
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationMode,
    DelegationRef,
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
)
from misaka_interaction_capability import (
    InteractionChannelStore,
    MessageNotFound,
)
from misaka_interaction_contracts import (
    InteractionChannelRef,
    InteractionMessage,
    InteractionMessageDraft,
    MessageCursor,
    MessageType,
    PrincipalKind,
    PrincipalRef,
)
from misaka_invocation_contracts import (
    CapabilityFeature,
    CompletionBoundary,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
)
from misaka_invocation_runtime import InvocationRuntime, RuntimeInvocationHandle
from misaka_kernel_contracts import JsonObject

from misaka_delegation_runtime.store import MemoryDelegationStore


@dataclass(slots=True)
class _ActiveActivation:
    invocation_id: str
    activation_number: int
    handle: RuntimeInvocationHandle
    bridge: asyncio.Task[None]


class DelegationRuntime(DelegationRuntimePort):
    """Compose Delegation, Interaction Channel and Invocation Runtime locally."""

    def __init__(
        self,
        invocation_runtime: InvocationRuntime,
        channel_store: InteractionChannelStore,
        *,
        store: DelegationStore | None = None,
    ) -> None:
        self.invocation_runtime = invocation_runtime
        self.channel_store = channel_store
        self.store: DelegationStore = store or MemoryDelegationStore()
        self._active: dict[str, _ActiveActivation] = {}
        self._lock = asyncio.Lock()
        self._stopping = False

    async def submit(self, request: DelegationRequest) -> DelegationHandle:
        if self._stopping:
            raise DelegationStateError(
                "delegation.runtime_stopping",
                "delegation runtime is stopping",
            )
        ref = self._initial_ref(request)
        snapshot, created = await self.store.create(request, ref)
        if not created:
            return _DelegationHandle(self, snapshot.ref.delegation_id)
        if ref.channel_id is not None:
            try:
                await self.channel_store.create(
                    InteractionChannelRef(ref.channel_id, request.scope)
                )
            except Exception as exc:
                await self._finalize_submission_failure(request.delegation_id, exc)
                return _DelegationHandle(self, request.delegation_id)
        if snapshot.ref != ref:
            await self.store.bind_ref(request.delegation_id, ref)
        try:
            await self._start_activation(request.delegation_id, request.input)
        except Exception as exc:
            await self._finalize_submission_failure(request.delegation_id, exc)
        return _DelegationHandle(self, request.delegation_id)

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot:
        return await self.store.snapshot(delegation_id)

    async def continue_request(self, request: ContinuationRequest) -> DelegationHandle:
        snapshot = await self.store.snapshot(request.delegation_id)
        self._authorize(snapshot, request.actor)
        if request.operation not in {
            ContinuationOperation.FOLLOW_UP,
            ContinuationOperation.CANCEL,
            ContinuationOperation.RECONCILE,
        }:
            raise DelegationCapabilityRejected(
                "delegation.operation_unsupported",
                "continuation operation "
                f"{request.operation.value} is not supported by local runtime",
            )
        fingerprint = _continuation_fingerprint(request)
        claimed = await self.store.claim_continuation(
            request.delegation_id,
            request.idempotency_key,
            fingerprint,
        )
        if not claimed:
            return _DelegationHandle(self, request.delegation_id)

        if request.operation is ContinuationOperation.FOLLOW_UP:
            return await self._follow_up(snapshot, request)
        if request.operation is ContinuationOperation.CANCEL:
            return await self._cancel(snapshot, request)
        if request.operation is ContinuationOperation.RECONCILE:
            active = self._active.get(request.delegation_id)
            if active is not None:
                await active.handle.reconcile()
            return _DelegationHandle(self, request.delegation_id)
        raise AssertionError("validated continuation operation was not dispatched")

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        active = tuple(self._active.values())
        await asyncio.gather(
            *(item.handle.cancel("delegation runtime stopping") for item in active),
            return_exceptions=True,
        )
        bridges = tuple(item.bridge for item in active)
        if bridges:
            await asyncio.gather(*bridges, return_exceptions=True)
        self._active.clear()

    async def _follow_up(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> DelegationHandle:
        if snapshot.request.mode is not DelegationMode.CONTINUABLE:
            raise DelegationStateError(
                "delegation.not_continuable",
                f"delegation {snapshot.ref.delegation_id} does not support follow-up",
            )
        if snapshot.ref.session_id != request.session_id:
            raise DelegationStateError(
                "delegation.session_mismatch",
                "continuation session does not match delegation session",
            )
        if snapshot.ref.channel_id is None:
            raise DelegationStateError(
                "delegation.channel_missing",
                "continuable delegation has no interaction channel",
            )
        try:
            message = await self.channel_store.get_message(
                snapshot.ref.channel_id,
                cast(str, request.message_id),
            )
        except MessageNotFound:
            message = await self.channel_store.publish(
                InteractionMessageDraft(
                    message_id=cast(str, request.message_id),
                    channel_id=snapshot.ref.channel_id,
                    sender=request.actor,
                    message_type=MessageType.ANSWER,
                    payload=request.input,
                    scope=snapshot.request.scope,
                )
            )
        if message.sender != request.actor:
            raise DelegationUnauthorized(
                "delegation.message_sender_mismatch",
                "continuation message is owned by another principal",
            )
        await self._start_activation(snapshot.ref.delegation_id, request.input)
        return _DelegationHandle(self, snapshot.ref.delegation_id)

    async def _cancel(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> DelegationHandle:
        active = self._active.get(snapshot.ref.delegation_id)
        if active is None:
            if snapshot.report is not None:
                return _DelegationHandle(self, snapshot.ref.delegation_id)
            raise DelegationStateError(
                "delegation.activation_unavailable",
                "active invocation handle is unavailable for cancellation",
            )
        reason = request.input.get("reason", "delegation cancelled")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancellation reason must be a non-empty string")
        await active.handle.cancel(reason)
        return _DelegationHandle(self, snapshot.ref.delegation_id)

    async def _start_activation(self, delegation_id: str, input_value: JsonObject) -> None:
        snapshot = await self.store.snapshot(delegation_id)
        required_features = _map_features(snapshot.request.required_features)
        invocation_id = f"{delegation_id}:activation:{snapshot.activation_count + 1}"
        activated = await self.store.activate(delegation_id, invocation_id)
        request = activated.request
        invocation_request = InvocationRequest(
            invocation_id=invocation_id,
            capability_id=request.capability_id,
            operation=request.operation,
            input=input_value,
            idempotency_key=f"{request.idempotency_key}:activation:{activated.activation_count}",
            completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
            required_features=required_features,
            output_schema=request.output_schema,
            policy_context=request.constraints,
            model=request.model,
            effort=request.effort,
        )
        try:
            handle = await self.invocation_runtime.submit(
                invocation_request,
                provider_id=request.provider_id,
            )
        except Exception as exc:
            await self.store.finalize(
                delegation_id,
                DelegationReport(
                    delegation_id=delegation_id,
                    status=DelegationStatus.FAILED,
                    error_code=getattr(exc, "code", type(exc).__name__),
                    error_message=str(exc),
                    source_activation_id=invocation_id,
                ),
            )
            raise
        async with self._lock:
            bridge = asyncio.create_task(
                self._bridge(activated, handle),
                name=f"delegation-bridge:{delegation_id}:{activated.activation_count}",
            )
            active = _ActiveActivation(invocation_id, activated.activation_count, handle, bridge)
            self._active[delegation_id] = active

    async def _finalize_submission_failure(self, delegation_id: str, error: Exception) -> None:
        snapshot = await self.store.snapshot(delegation_id)
        if snapshot.report is not None:
            return
        status = (
            DelegationStatus.REJECTED
            if isinstance(error, DelegationCapabilityRejected)
            else DelegationStatus.FAILED
        )
        await self.store.finalize(
            delegation_id,
            DelegationReport(
                delegation_id=delegation_id,
                status=status,
                error_code=getattr(error, "code", type(error).__name__),
                error_message=str(error),
            ),
        )

    async def _bridge(
        self,
        snapshot: DelegationSnapshot,
        handle: RuntimeInvocationHandle,
    ) -> None:
        delegation_id = snapshot.ref.delegation_id
        try:
            async for event in handle.events():
                await self._publish_invocation_event(snapshot, event)
            result = await handle.wait()
            report = _report_from_result(delegation_id, snapshot.current_invocation_id, result)
            await self.store.finalize(delegation_id, report)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await self.store.finalize(
                    delegation_id,
                    DelegationReport(
                        delegation_id=delegation_id,
                        status=DelegationStatus.RECONCILIATION_REQUIRED,
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=str(exc),
                        source_activation_id=snapshot.current_invocation_id,
                    ),
                )
            except Exception:
                pass
        finally:
            async with self._lock:
                active = self._active.get(delegation_id)
                if active is not None and active.handle is handle:
                    self._active.pop(delegation_id, None)

    async def _publish_invocation_event(
        self,
        snapshot: DelegationSnapshot,
        event: object,
    ) -> None:
        if snapshot.ref.channel_id is None:
            return
        invocation_event = cast("InvocationEventLike", event)
        status = invocation_event.status
        message_type = (
            MessageType.RESULT
            if status
            in {
                InvocationStatus.SUCCEEDED,
                InvocationStatus.REJECTED,
                InvocationStatus.FAILED,
                InvocationStatus.CANCELLED,
                InvocationStatus.RECONCILIATION_REQUIRED,
            }
            else MessageType.PROGRESS
        )
        try:
            await self.channel_store.publish(
                InteractionMessageDraft(
                    message_id=(
                        f"{snapshot.ref.delegation_id}:activation:"
                        f"{snapshot.activation_count}:event:{invocation_event.sequence}"
                    ),
                    channel_id=snapshot.ref.channel_id,
                    sender=PrincipalRef(
                        f"delegation:{snapshot.ref.delegation_id}",
                        PrincipalKind.AGENT,
                    ),
                    recipient=snapshot.request.controller,
                    message_type=message_type,
                    payload=cast(
                        JsonObject,
                        {
                            "activation_id": snapshot.current_invocation_id,
                            "status": status.value,
                            "payload": invocation_event.payload,
                        },
                    ),
                    scope=snapshot.request.scope,
                    correlation_id=snapshot.ref.delegation_id,
                )
            )
        except Exception:
            # Delivery is an observation path; execution facts remain authoritative.
            return

    def _initial_ref(self, request: DelegationRequest) -> DelegationRef:
        session_id = request.session_id
        channel_id = request.channel_id
        if request.mode is DelegationMode.CONTINUABLE:
            session_id = session_id or f"delegation-session:{request.delegation_id}"
            channel_id = channel_id or f"delegation-channel:{request.delegation_id}"
        return DelegationRef(
            delegation_id=request.delegation_id,
            session_id=session_id,
            channel_id=channel_id,
            parent_delegation_id=request.parent_delegation_id,
        )

    @staticmethod
    def _authorize(snapshot: DelegationSnapshot, actor: PrincipalRef) -> None:
        allowed = {
            snapshot.request.initiator.principal_id,
            snapshot.request.controller.principal_id,
        }
        if actor.principal_id not in allowed:
            raise DelegationUnauthorized(
                "delegation.actor_forbidden",
                "principal "
                f"{actor.principal_id} cannot control delegation {snapshot.ref.delegation_id}",
            )


class _DelegationHandle:
    def __init__(self, runtime: DelegationRuntime, delegation_id: str) -> None:
        self._runtime = runtime
        self._delegation_id = delegation_id

    @property
    def delegation_id(self) -> str:
        return self._delegation_id

    async def wait(self) -> DelegationReport:
        return await self._runtime.store.wait_terminal(self._delegation_id)

    async def snapshot(self) -> DelegationSnapshot:
        return await self._runtime.snapshot(self._delegation_id)

    def messages(self, *, cursor: MessageCursor | None = None) -> AsyncIterator[InteractionMessage]:
        return self._messages(cursor=cursor)

    async def _messages(
        self, *, cursor: MessageCursor | None = None
    ) -> AsyncIterator[InteractionMessage]:
        snapshot = await self.snapshot()
        if snapshot.ref.channel_id is None:
            return
        async for message in self._runtime.channel_store.events(
            snapshot.ref.channel_id,
            cursor=cursor,
        ):
            yield message

    async def continue_request(self, request: ContinuationRequest) -> DelegationHandle:
        return await self._runtime.continue_request(request)

    async def cancel(self, actor_id: str, reason: str) -> None:
        snapshot = await self.snapshot()
        actor = PrincipalRef(actor_id, PrincipalKind.APPLICATION)
        await self._runtime.continue_request(
            ContinuationRequest(
                request_id=f"{self._delegation_id}:cancel:{hashlib.sha256(reason.encode()).hexdigest()[:12]}",
                delegation_id=self._delegation_id,
                operation=ContinuationOperation.CANCEL,
                actor=actor,
                idempotency_key=f"{self._delegation_id}:cancel:{hashlib.sha256(reason.encode()).hexdigest()}",
                session_id=snapshot.ref.session_id,
                input={"reason": reason},
            )
        )


class InvocationEventLike:
    sequence: int
    status: InvocationStatus
    payload: JsonObject


def _map_features(values: frozenset[str]) -> frozenset[CapabilityFeature]:
    mapped: set[CapabilityFeature] = set()
    for value in values:
        try:
            mapped.add(CapabilityFeature(value))
        except ValueError as exc:
            raise DelegationCapabilityRejected(
                "delegation.feature_unsupported",
                f"delegation requires unknown capability feature {value}",
            ) from exc
    return frozenset(mapped)


def _report_from_result(
    delegation_id: str,
    activation_id: str | None,
    result: InvocationResult,
) -> DelegationReport:
    status_map = {
        InvocationStatus.SUCCEEDED: DelegationStatus.COMPLETED,
        InvocationStatus.REJECTED: DelegationStatus.REJECTED,
        InvocationStatus.FAILED: DelegationStatus.FAILED,
        InvocationStatus.CANCELLED: DelegationStatus.CANCELLED,
        InvocationStatus.RECONCILIATION_REQUIRED: DelegationStatus.RECONCILIATION_REQUIRED,
    }
    return DelegationReport(
        delegation_id=delegation_id,
        status=status_map[result.status],
        output=result.output,
        artifact_ids=tuple(artifact.artifact_id for artifact in result.artifacts),
        error_code=result.error_code,
        error_message=result.error_message,
        source_activation_id=activation_id,
    )


def _continuation_fingerprint(request: ContinuationRequest) -> str:
    payload = {
        "delegation_id": request.delegation_id,
        "operation": request.operation.value,
        "actor": request.actor.principal_id,
        "session_id": request.session_id,
        "message_id": request.message_id,
        "expected_activation_id": request.expected_activation_id,
        "input": request.input,
        "correlation_id": request.correlation_id,
        "reply_to": request.reply_to,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
