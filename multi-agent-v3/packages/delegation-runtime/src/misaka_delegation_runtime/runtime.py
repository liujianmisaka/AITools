from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from misaka_delegation_capability import (
    AllowAllDelegationGate,
    DelegationCapabilityRejected,
    DelegationExecutionHandle,
    DelegationExecutionPort,
    DelegationGate,
    DelegationHandle,
    DelegationNotFound,
    DelegationRuntimePort,
    DelegationStateError,
    DelegationStore,
    DelegationUnauthorized,
)
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationOperationSpec,
    ContinuationRequest,
    DelegationAdmission,
    DelegationMode,
    DelegationRef,
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
    continuation_operation_spec,
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
    MessageDeliveryStatus,
    MessageType,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_invocation_contracts import (
    CapabilityFeature,
    CompletionBoundary,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    SessionRef,
)
from misaka_kernel_contracts import JsonObject, JsonValue
from misaka_persistence_contracts import (
    DurableConflict,
    DurableNotFound,
    SessionHeader,
    SessionLog,
)

from misaka_delegation_runtime.session_events import (
    DelegationSessionEventKind,
    DelegationSessionEventSink,
)
from misaka_delegation_runtime.store import MemoryDelegationStore


@dataclass(slots=True)
class _ActiveActivation:
    invocation_id: str
    activation_id: str
    activation_number: int
    handle: DelegationExecutionHandle
    bridge: asyncio.Task[None]
    message_id: str | None = None
    reply_target_id: str | None = None


@dataclass(slots=True)
class _PreparedActivation:
    invocation_id: str
    activation_id: str
    activation_number: int
    request: InvocationRequest
    message_id: str | None = None
    reply_target_id: str | None = None


class DelegationRuntime(DelegationRuntimePort):
    """Compose Delegation, Interaction Channel and Invocation Runtime locally."""

    def __init__(
        self,
        invocation_runtime: DelegationExecutionPort,
        channel_store: InteractionChannelStore,
        *,
        store: DelegationStore | None = None,
        gate: DelegationGate | None = None,
        session_log: SessionLog | None = None,
        composition_id: str | None = None,
        session_events: DelegationSessionEventSink | None = None,
    ) -> None:
        if composition_id is not None and not composition_id.strip():
            raise ValueError("composition_id must not be empty when provided")
        if session_log is not None and composition_id is None:
            raise ValueError("composition_id is required when session_log is configured")
        self.invocation_runtime = invocation_runtime
        self.channel_store = channel_store
        self.store: DelegationStore = store or MemoryDelegationStore()
        self.gate = gate or AllowAllDelegationGate()
        self.session_log = session_log
        self.composition_id = composition_id
        self.session_events = session_events
        self._active: dict[str, _ActiveActivation] = {}
        self._prepared: dict[str, _PreparedActivation] = {}
        self._lock = asyncio.Lock()
        self._activation_locks: dict[str, asyncio.Lock] = {}
        self._stopping = False

    async def submit(self, request: DelegationRequest) -> DelegationHandle:
        if self._stopping:
            raise DelegationStateError(
                "delegation.runtime_stopping",
                "delegation runtime is stopping",
            )
        parent_error: Exception | None = None
        try:
            parent = await self._parent_snapshot(request)
            ref = self._initial_ref(request, parent)
        except (
            DelegationCapabilityRejected,
            DelegationStateError,
            DelegationUnauthorized,
        ) as exc:
            parent = None
            parent_error = exc
            ref = self._fallback_ref(request)
        snapshot, created = await self.store.create(request, ref)
        if not created:
            return _DelegationHandle(self, snapshot.ref.delegation_id)
        await self._publish_session_event(
            request.delegation_id,
            event_id=f"{request.delegation_id}:lifecycle:created",
            kind=DelegationSessionEventKind.LIFECYCLE,
            status=snapshot.status.value,
            payload={"stage": "created"},
        )
        if parent_error is not None:
            admission = DelegationAdmission(
                allowed=False,
                reason=str(parent_error),
                error_code=getattr(parent_error, "code", type(parent_error).__name__),
            )
            await self.store.record_admission(request.delegation_id, admission)
            report = DelegationReport(
                delegation_id=request.delegation_id,
                status=DelegationStatus.REJECTED,
                error_code=admission.error_code,
                error_message=admission.reason,
            )
            await self.store.finalize(
                request.delegation_id,
                report,
            )
            await self._publish_terminal_report(request, report)
            return _DelegationHandle(self, request.delegation_id)
        try:
            admission = await self.gate.evaluate(request, parent)
            await self.store.record_admission(request.delegation_id, admission)
            await self._publish_session_event(
                request.delegation_id,
                event_id=f"{request.delegation_id}:lifecycle:admission",
                kind=DelegationSessionEventKind.LIFECYCLE,
                status=(
                    DelegationStatus.ADMITTED.value
                    if admission.allowed
                    else DelegationStatus.REJECTED.value
                ),
                payload={
                    "stage": "admission",
                    "allowed": admission.allowed,
                    "reason": admission.reason,
                    "error_code": admission.error_code,
                },
            )
            if not admission.allowed:
                report = DelegationReport(
                    delegation_id=request.delegation_id,
                    status=DelegationStatus.REJECTED,
                    error_code=admission.error_code or "delegation.rejected",
                    error_message=admission.reason,
                )
                await self.store.finalize(
                    request.delegation_id,
                    report,
                )
                await self._publish_terminal_report(request, report)
                return _DelegationHandle(self, request.delegation_id)
            if parent is not None:
                await self.store.attach_child(parent.ref.delegation_id, ref)
        except Exception as exc:
            await self._finalize_submission_failure(request.delegation_id, exc)
            return _DelegationHandle(self, request.delegation_id)
        if ref.channel_id is not None:
            try:
                await self.channel_store.create(
                    InteractionChannelRef(
                        ref.channel_id,
                        ref.child_scope or request.scope,
                    )
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

    async def children(self, delegation_id: str) -> tuple[DelegationSnapshot, ...]:
        return await self.store.list_children(delegation_id)

    async def read_messages(
        self,
        delegation_id: str,
        *,
        cursor: MessageCursor | None = None,
    ) -> tuple[InteractionMessage, ...]:
        snapshot = await self.store.snapshot(delegation_id)
        if snapshot.ref.channel_id is None:
            return ()
        return await self.channel_store.read(snapshot.ref.channel_id, cursor=cursor)

    async def send_message(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        draft: InteractionMessageDraft,
    ) -> InteractionMessage:
        snapshot = await self.store.snapshot(delegation_id)
        self._authorize(snapshot, actor)
        if snapshot.ref.channel_id is None:
            raise DelegationStateError(
                "delegation.channel_missing",
                "delegation has no interaction channel",
            )
        expected_scope = snapshot.ref.child_scope or snapshot.request.scope
        if draft.channel_id != snapshot.ref.channel_id:
            raise DelegationStateError(
                "delegation.channel_mismatch",
                "message channel does not belong to delegation",
            )
        if draft.sender != actor:
            raise DelegationUnauthorized(
                "delegation.message_sender_forbidden",
                "message sender does not match the controlling principal",
            )
        if draft.scope != expected_scope:
            raise DelegationUnauthorized(
                "delegation.message_scope_forbidden",
                "message scope is outside the delegation child scope",
            )
        if (
            draft.message_type is MessageType.QUESTION
            and actor.principal_id == f"delegation:{snapshot.ref.delegation_id}"
            and snapshot.status is not DelegationStatus.WAITING_INPUT
        ):
            if snapshot.current_invocation_id is not None:
                raise DelegationStateError(
                    "delegation.activation_active",
                    "a live activation must be paused before asking for input",
                )
            if snapshot.status is not DelegationStatus.COMPLETED:
                raise DelegationStateError(
                    "delegation.waiting_input_state_invalid",
                    "only a completed activation can wait for input",
                )
        message = await self.channel_store.publish(draft)
        if (
            draft.message_type is MessageType.QUESTION
            and actor.principal_id == f"delegation:{snapshot.ref.delegation_id}"
        ):
            await self.store.mark_waiting_input(delegation_id, message.message_id)
        return message

    async def transition_message(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        message_id: str,
        status: MessageDeliveryStatus,
        *,
        expected_status: MessageDeliveryStatus | None = None,
    ) -> InteractionMessage:
        snapshot = await self.store.snapshot(delegation_id)
        if snapshot.ref.channel_id is None:
            raise DelegationStateError(
                "delegation.channel_missing",
                "delegation has no interaction channel",
            )
        message = await self.channel_store.get_message(snapshot.ref.channel_id, message_id)
        if not self._can_transition_message(snapshot, message, actor):
            raise DelegationUnauthorized(
                "delegation.message_transition_forbidden",
                "principal cannot transition this interaction message",
            )
        return await self.channel_store.transition(
            snapshot.ref.channel_id,
            message_id,
            status,
            expected_status=expected_status,
        )

    async def continue_request(self, request: ContinuationRequest) -> DelegationHandle:
        snapshot = await self.store.snapshot(request.delegation_id)
        self._authorize(snapshot, request.actor)
        spec = continuation_operation_spec(request.operation)
        fingerprint = _continuation_fingerprint(request)
        existing_fingerprint = await self.store.continuation_fingerprint(
            request.delegation_id, request.idempotency_key
        )
        if existing_fingerprint is not None:
            if existing_fingerprint != fingerprint:
                raise DelegationStateError(
                    "delegation.continuation_conflict",
                    "continuation idempotency key has a different request",
                )
            return _DelegationHandle(self, request.delegation_id)
        self._validate_operation_references(snapshot, request, spec)
        self._validate_expected_activation(snapshot, request, spec)
        await self._validate_operation_channel(snapshot, spec)
        if request.operation in {
            ContinuationOperation.FOLLOW_UP,
            ContinuationOperation.REPLY,
        }:
            self._validate_follow_up(snapshot, request)
        if request.operation is ContinuationOperation.PREPARE:
            self._validate_prepare(snapshot, request)
        if request.operation is ContinuationOperation.START:
            self._validate_start(snapshot, request)
        if request.operation is ContinuationOperation.STEER:
            await self._validate_live_control(snapshot, request, "steer")
        if request.operation is ContinuationOperation.PAUSE:
            await self._validate_live_control(snapshot, request, "pause")
        if request.operation is ContinuationOperation.RESUME:
            await self._validate_live_control(snapshot, request, "resume")
        if request.operation is ContinuationOperation.ACK:
            await self._validate_ack(snapshot, request)
        if request.operation is ContinuationOperation.CLOSE:
            await self._validate_close(snapshot, request)
        claimed = await self.store.claim_continuation(
            request.delegation_id,
            request.idempotency_key,
            fingerprint,
        )
        if not claimed:
            return _DelegationHandle(self, request.delegation_id)

        if request.operation is ContinuationOperation.PREPARE:
            await self._prepare_activation(request.delegation_id, request.input)
            return _DelegationHandle(self, request.delegation_id)
        if request.operation is ContinuationOperation.START:
            await self._start_prepared_activation(request.delegation_id)
            return _DelegationHandle(self, request.delegation_id)
        if request.operation is ContinuationOperation.STEER:
            return await self._steer(snapshot, request)
        if request.operation is ContinuationOperation.PAUSE:
            return await self._pause(snapshot, request)
        if request.operation is ContinuationOperation.RESUME:
            return await self._resume(snapshot, request)
        if request.operation is ContinuationOperation.ACK:
            return await self._acknowledge(snapshot, request)
        if request.operation is ContinuationOperation.CLOSE:
            return await self._close(snapshot, request)
        if request.operation in {
            ContinuationOperation.FOLLOW_UP,
            ContinuationOperation.REPLY,
        }:
            return await self._follow_up(snapshot, request)
        if request.operation is ContinuationOperation.CANCEL:
            return await self._cancel(snapshot, request)
        if request.operation is ContinuationOperation.RECONCILE:
            active = self._active.get(request.delegation_id)
            if active is not None:
                await active.handle.reconcile()
            return _DelegationHandle(self, request.delegation_id)
        raise AssertionError("validated continuation operation was not dispatched")

    async def recover(self) -> tuple[DelegationSnapshot, ...]:
        """Resume safe pre-start facts and fence uncertain external activations."""

        if self._stopping:
            raise DelegationStateError(
                "delegation.runtime_stopping",
                "delegation runtime is stopping",
            )
        handled_ids: set[str] = set()
        snapshots = sorted(
            await self.store.list(),
            key=lambda item: (item.ref.depth, item.ref.delegation_id),
        )
        for snapshot in snapshots:
            if snapshot.report is not None or snapshot.status is not DelegationStatus.PROPOSED:
                continue
            await self._recover_proposed(snapshot)
            handled_ids.add(snapshot.ref.delegation_id)

        uncertain_statuses = {
            DelegationStatus.PREPARING,
            DelegationStatus.ACTIVE,
            DelegationStatus.PAUSED,
            DelegationStatus.RECONCILING,
        }
        snapshots = sorted(
            await self.store.list(),
            key=lambda item: (item.ref.depth, item.ref.delegation_id),
        )
        for snapshot in snapshots:
            if snapshot.report is not None:
                continue
            if snapshot.status is DelegationStatus.ADMITTED:
                await self._recover_admitted(snapshot)
                handled_ids.add(snapshot.ref.delegation_id)
                continue
            if snapshot.status not in uncertain_statuses:
                continue
            report = DelegationReport(
                delegation_id=snapshot.ref.delegation_id,
                status=DelegationStatus.RECONCILIATION_REQUIRED,
                error_code="delegation.recovery_activation_unavailable",
                error_message=(
                    "delegation runtime recovered without a live activation handle; "
                    f"last durable status was {snapshot.status.value}"
                ),
                source_invocation_id=snapshot.current_invocation_id,
                source_activation_id=snapshot.current_activation_id,
            )
            await self.store.finalize(snapshot.ref.delegation_id, report)
            await self._publish_child_report(snapshot, report)
            await self._close_one_shot_channel(snapshot)
            handled_ids.add(snapshot.ref.delegation_id)
        return tuple(
            [await self.store.snapshot(delegation_id) for delegation_id in sorted(handled_ids)]
        )

    async def _recover_proposed(self, snapshot: DelegationSnapshot) -> None:
        request = snapshot.request
        admission = snapshot.admission
        if admission is not None:
            if admission.allowed:
                raise DelegationStateError(
                    "delegation.recovery_admission_state_invalid",
                    "an allowed admission must have advanced the delegation state",
                )
            await self.store.finalize(
                request.delegation_id,
                DelegationReport(
                    delegation_id=request.delegation_id,
                    status=DelegationStatus.REJECTED,
                    error_code=admission.error_code or "delegation.rejected",
                    error_message=admission.reason,
                ),
            )
            await self._close_one_shot_channel(snapshot)
            return
        try:
            parent = await self._parent_snapshot(request)
            admission = await self.gate.evaluate(request, parent)
            await self.store.record_admission(request.delegation_id, admission)
            if not admission.allowed:
                await self.store.finalize(
                    request.delegation_id,
                    DelegationReport(
                        delegation_id=request.delegation_id,
                        status=DelegationStatus.REJECTED,
                        error_code=admission.error_code or "delegation.rejected",
                        error_message=admission.reason,
                    ),
                )
                await self._close_one_shot_channel(snapshot)
                return
            if parent is not None:
                await self.store.attach_child(parent.ref.delegation_id, snapshot.ref)
        except Exception as exc:
            await self._finalize_submission_failure(request.delegation_id, exc)

    async def _recover_admitted(self, snapshot: DelegationSnapshot) -> None:
        try:
            if snapshot.ref.parent_delegation_id is not None:
                await self.store.attach_child(
                    snapshot.ref.parent_delegation_id,
                    snapshot.ref,
                )
            if snapshot.ref.channel_id is not None:
                await self.channel_store.create(
                    InteractionChannelRef(
                        snapshot.ref.channel_id,
                        snapshot.ref.child_scope or snapshot.request.scope,
                    )
                )
            await self._start_activation(
                snapshot.ref.delegation_id,
                snapshot.request.input,
            )
        except Exception as exc:
            await self._finalize_submission_failure(snapshot.ref.delegation_id, exc)

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
        for delegation_id, prepared in tuple(self._prepared.items()):
            try:
                await self.store.finalize(
                    delegation_id,
                    DelegationReport(
                        delegation_id=delegation_id,
                        status=DelegationStatus.CANCELLED,
                        error_code="delegation.runtime_stopping",
                        error_message="delegation runtime stopped before activation start",
                        source_invocation_id=prepared.invocation_id,
                        source_activation_id=prepared.activation_id,
                    ),
                )
            except Exception:
                pass
        self._prepared.clear()
        self._activation_locks.clear()

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
        if snapshot.current_invocation_id is not None:
            raise DelegationStateError(
                "delegation.activation_active",
                "follow-up cannot start while the current activation is live",
            )
        if snapshot.status is not DelegationStatus.WAITING_INPUT and snapshot.report is None:
            raise DelegationStateError(
                "delegation.activation_not_terminal",
                "follow-up requires a completed current activation",
            )
        if snapshot.activation_count >= snapshot.request.policy.budget.max_activations:
            raise DelegationStateError(
                "delegation.activation_budget_exceeded",
                "follow-up exceeds the delegation activation budget",
            )
        if snapshot.ref.channel_id is None:
            raise DelegationStateError(
                "delegation.channel_missing",
                "continuable delegation has no interaction channel",
            )
        reply_target: InteractionMessage | None = None
        if request.reply_to is not None:
            try:
                reply_target = await self.channel_store.get_message(
                    snapshot.ref.channel_id, request.reply_to
                )
            except MessageNotFound as exc:
                raise DelegationStateError(
                    "delegation.reply_target_missing",
                    f"reply target {request.reply_to} was not found",
                ) from exc
            if reply_target.message_type is not MessageType.QUESTION:
                raise DelegationStateError(
                    "delegation.reply_target_invalid",
                    "reply target must be a question message",
                )
            if reply_target.delivery_status in {
                MessageDeliveryStatus.COMPLETED,
                MessageDeliveryStatus.REJECTED,
                MessageDeliveryStatus.EXPIRED,
            }:
                raise DelegationStateError(
                    "delegation.reply_target_completed",
                    "reply target has already reached a terminal delivery state",
                )
            if reply_target.correlation_id != request.correlation_id:
                raise DelegationStateError(
                    "delegation.reply_correlation_mismatch",
                    "reply correlation does not match the question",
                )
            if reply_target.recipient is not None and reply_target.recipient != request.actor:
                raise DelegationUnauthorized(
                    "delegation.reply_recipient_mismatch",
                    "reply actor is not the question recipient",
                )
        try:
            message = await self.channel_store.get_message(
                snapshot.ref.channel_id,
                cast(str, request.message_id),
            )
        except MessageNotFound:
            message = await self.send_message(
                snapshot.ref.delegation_id,
                request.actor,
                InteractionMessageDraft(
                    message_id=cast(str, request.message_id),
                    channel_id=snapshot.ref.channel_id,
                    sender=request.actor,
                    recipient=PrincipalRef(
                        f"delegation:{snapshot.ref.delegation_id}",
                        PrincipalKind.AGENT,
                    ),
                    message_type=MessageType.ANSWER,
                    payload=request.input,
                    scope=snapshot.ref.child_scope or snapshot.request.scope,
                    correlation_id=request.correlation_id or snapshot.ref.delegation_id,
                    reply_to=request.reply_to,
                ),
            )
        if message.sender != request.actor:
            raise DelegationUnauthorized(
                "delegation.message_sender_mismatch",
                "continuation message is owned by another principal",
            )
        if message.message_type is not MessageType.ANSWER:
            raise DelegationStateError(
                "delegation.continuation_message_invalid",
                "continuation message must be an answer",
            )
        if message.scope != snapshot.ref.child_scope and message.scope != snapshot.request.scope:
            raise DelegationUnauthorized(
                "delegation.message_scope_forbidden",
                "continuation message is outside the delegation scope",
            )
        if message.delivery_status in {
            MessageDeliveryStatus.COMPLETED,
            MessageDeliveryStatus.REJECTED,
            MessageDeliveryStatus.EXPIRED,
        }:
            raise DelegationStateError(
                "delegation.continuation_message_completed",
                "continuation message has already reached a terminal delivery state",
            )
        if request.correlation_id is not None and message.correlation_id != request.correlation_id:
            raise DelegationStateError(
                "delegation.continuation_correlation_mismatch",
                "continuation correlation does not match the message",
            )
        if request.reply_to is not None and message.reply_to != request.reply_to:
            raise DelegationStateError(
                "delegation.continuation_reply_target_mismatch",
                "continuation reply target does not match the message",
            )
        message = await self._mark_message_processed(snapshot, request.actor, message)
        await self._start_activation(
            snapshot.ref.delegation_id,
            request.input,
            message_id=message.message_id,
            reply_target_id=reply_target.message_id if reply_target is not None else None,
        )
        return _DelegationHandle(self, snapshot.ref.delegation_id)

    async def _steer(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> DelegationHandle:
        activation_lock = self._activation_locks.setdefault(
            snapshot.ref.delegation_id, asyncio.Lock()
        )
        async with activation_lock:
            message = await self._control_message(snapshot, request, MessageType.STEER)
            message = await self._mark_message_processed(snapshot, request.actor, message)
            active = self._require_active_activation(snapshot.ref.delegation_id)
            operation = self._require_control_method(active, "steer")
            await operation(request.input)
            await self._complete_message(snapshot, message.message_id)
        return _DelegationHandle(self, snapshot.ref.delegation_id)

    async def _pause(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> DelegationHandle:
        activation_lock = self._activation_locks.setdefault(
            snapshot.ref.delegation_id, asyncio.Lock()
        )
        async with activation_lock:
            active = self._require_active_activation(snapshot.ref.delegation_id)
            operation = self._require_control_method(active, "pause")
            await operation(request.input)
            await self.store.mark_activation_paused(
                snapshot.ref.delegation_id,
                active.invocation_id,
                active.activation_id,
            )
        return _DelegationHandle(self, snapshot.ref.delegation_id)

    async def _resume(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> DelegationHandle:
        activation_lock = self._activation_locks.setdefault(
            snapshot.ref.delegation_id, asyncio.Lock()
        )
        async with activation_lock:
            active = self._require_active_activation(snapshot.ref.delegation_id)
            operation = self._require_control_method(active, "resume")
            await operation(request.input)
            await self.store.mark_activation_resumed(
                snapshot.ref.delegation_id,
                active.invocation_id,
                active.activation_id,
            )
        return _DelegationHandle(self, snapshot.ref.delegation_id)

    async def _acknowledge(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> DelegationHandle:
        channel_id = cast(str, snapshot.ref.channel_id)
        target_id = cast(str, request.reply_to)
        target = await self.channel_store.get_message(channel_id, target_id)
        acknowledgement = await self._control_message(
            snapshot,
            request,
            MessageType.ACK,
            recipient=target.sender,
        )
        await self._complete_message(snapshot, target.message_id)
        await self._complete_message(snapshot, acknowledgement.message_id)
        return _DelegationHandle(self, snapshot.ref.delegation_id)

    async def _close(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> DelegationHandle:
        delegation_id = snapshot.ref.delegation_id
        activation_lock = self._activation_locks.setdefault(delegation_id, asyncio.Lock())
        async with activation_lock:
            channel_id = cast(str, snapshot.ref.channel_id)
            current = await self.store.snapshot(delegation_id)
            if current.current_invocation_id is not None or delegation_id in self._active:
                raise DelegationStateError(
                    "delegation.close_activation_live",
                    "cancel or finish the current activation before closing the session",
                )
            prepared = self._prepared.pop(delegation_id, None)
            if prepared is not None:
                reason = request.input.get("reason", "delegation session closed")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("close reason must be a non-empty string")
                await self.store.finalize(
                    delegation_id,
                    DelegationReport(
                        delegation_id=delegation_id,
                        status=DelegationStatus.CANCELLED,
                        error_code="delegation.closed",
                        error_message=reason,
                        source_invocation_id=prepared.invocation_id,
                        source_activation_id=prepared.activation_id,
                    ),
                )
                current = await self.store.snapshot(delegation_id)
            elif current.report is None:
                previous = current.report_history[-1] if current.report_history else None
                reason = request.input.get("reason", "delegation session closed")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("close reason must be a non-empty string")
                await self.store.finalize(
                    delegation_id,
                    DelegationReport(
                        delegation_id=delegation_id,
                        status=DelegationStatus.CANCELLED,
                        error_code="delegation.closed",
                        error_message=reason,
                        source_invocation_id=(
                            previous.source_invocation_id if previous is not None else None
                        ),
                        source_activation_id=(
                            previous.source_activation_id if previous is not None else None
                        ),
                    ),
                )
            await self.channel_store.close(channel_id)
            if self.session_events is not None:
                try:
                    await self.session_events.close_session(
                        delegation_id,
                        event_id=f"{delegation_id}:session:closed",
                        status=current.status.value,
                    )
                except Exception:
                    pass
            self._activation_locks.pop(delegation_id, None)
        return _DelegationHandle(self, current.ref.delegation_id)

    async def _mark_message_processed(
        self,
        snapshot: DelegationSnapshot,
        actor: PrincipalRef,
        message: InteractionMessage,
    ) -> InteractionMessage:
        current = message
        if current.delivery_status is MessageDeliveryStatus.ACCEPTED:
            current = await self.transition_message(
                snapshot.ref.delegation_id,
                actor,
                current.message_id,
                MessageDeliveryStatus.DELIVERED,
                expected_status=MessageDeliveryStatus.ACCEPTED,
            )
        if current.delivery_status is MessageDeliveryStatus.DELIVERED:
            current = await self.transition_message(
                snapshot.ref.delegation_id,
                actor,
                current.message_id,
                MessageDeliveryStatus.PROCESSED,
                expected_status=MessageDeliveryStatus.DELIVERED,
            )
        if current.delivery_status not in {
            MessageDeliveryStatus.PROCESSED,
            MessageDeliveryStatus.COMPLETED,
        }:
            raise DelegationStateError(
                "delegation.message_not_processable",
                f"message {current.message_id} is {current.delivery_status.value}",
            )
        return current

    async def _control_message(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
        message_type: MessageType,
        *,
        recipient: PrincipalRef | None = None,
    ) -> InteractionMessage:
        channel_id = cast(str, snapshot.ref.channel_id)
        message_id = cast(str, request.message_id)
        expected_recipient = recipient or PrincipalRef(
            f"delegation:{snapshot.ref.delegation_id}",
            PrincipalKind.AGENT,
        )
        try:
            message = await self.channel_store.get_message(channel_id, message_id)
        except MessageNotFound:
            message = await self.send_message(
                snapshot.ref.delegation_id,
                request.actor,
                InteractionMessageDraft(
                    message_id=message_id,
                    channel_id=channel_id,
                    sender=request.actor,
                    recipient=expected_recipient,
                    message_type=message_type,
                    payload=request.input,
                    scope=snapshot.ref.child_scope or snapshot.request.scope,
                    correlation_id=request.correlation_id or snapshot.ref.delegation_id,
                    reply_to=request.reply_to,
                ),
            )
        if message.sender != request.actor or message.recipient != expected_recipient:
            raise DelegationUnauthorized(
                "delegation.control_message_owner_mismatch",
                "control message ownership does not match the continuation request",
            )
        if message.message_type is not message_type:
            raise DelegationStateError(
                "delegation.control_message_type_mismatch",
                f"control message {message.message_id} has type {message.message_type.value}",
            )
        if message.payload != request.input or message.reply_to != request.reply_to:
            raise DelegationStateError(
                "delegation.control_message_conflict",
                "control message content does not match the continuation request",
            )
        if request.correlation_id is not None and message.correlation_id != request.correlation_id:
            raise DelegationStateError(
                "delegation.control_message_correlation_mismatch",
                "control message correlation does not match the continuation request",
            )
        return message

    def _require_active_activation(self, delegation_id: str) -> _ActiveActivation:
        active = self._active.get(delegation_id)
        if active is None:
            raise DelegationStateError(
                "delegation.activation_unavailable",
                "active invocation handle is unavailable for continuation control",
            )
        return active

    @staticmethod
    def _require_control_method(
        active: _ActiveActivation,
        operation_name: str,
    ) -> Callable[[JsonObject], Awaitable[None]]:
        operation = getattr(active.handle, operation_name, None)
        if not callable(operation):
            raise DelegationCapabilityRejected(
                f"delegation.{operation_name}_unsupported",
                f"active provider does not support {operation_name}",
            )
        return cast(Callable[[JsonObject], Awaitable[None]], operation)

    async def _cancel(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> DelegationHandle:
        reason = request.input.get("reason", "delegation cancelled")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancellation reason must be a non-empty string")
        delegation_id = snapshot.ref.delegation_id
        activation_lock = self._activation_locks.setdefault(delegation_id, asyncio.Lock())
        async with activation_lock:
            current = await self.store.snapshot(delegation_id)
            active = self._active.get(delegation_id)
            prepared = self._prepared.pop(delegation_id, None) if active is None else None
            if prepared is not None:
                report = DelegationReport(
                    delegation_id=delegation_id,
                    status=DelegationStatus.CANCELLED,
                    error_code="delegation.cancelled",
                    error_message=reason,
                    source_invocation_id=prepared.invocation_id,
                    source_activation_id=prepared.activation_id,
                )
                await self.store.finalize(
                    delegation_id,
                    report,
                )
                await self._publish_terminal_report(
                    current.request,
                    report,
                    invocation_id=prepared.invocation_id,
                    activation_id=prepared.activation_id,
                    activation_number=prepared.activation_number,
                )
                self._activation_locks.pop(delegation_id, None)
                return _DelegationHandle(self, delegation_id)
            if active is not None:
                await active.handle.cancel(reason)
                return _DelegationHandle(self, delegation_id)
            if current.status is DelegationStatus.WAITING_INPUT:
                previous = current.report_history[-1] if current.report_history else None
                report = DelegationReport(
                    delegation_id=delegation_id,
                    status=DelegationStatus.CANCELLED,
                    error_code="delegation.cancelled",
                    error_message=reason,
                    source_invocation_id=(
                        previous.source_invocation_id if previous is not None else None
                    ),
                    source_activation_id=(
                        previous.source_activation_id if previous is not None else None
                    ),
                )
                await self.store.finalize(
                    delegation_id,
                    report,
                )
                await self._publish_terminal_report(current.request, report)
                return _DelegationHandle(self, delegation_id)
            if current.report is not None:
                return _DelegationHandle(self, delegation_id)
            raise DelegationStateError(
                "delegation.activation_unavailable",
                "active invocation handle is unavailable for cancellation",
            )

    async def _start_activation(
        self,
        delegation_id: str,
        input_value: JsonObject,
        *,
        message_id: str | None = None,
        reply_target_id: str | None = None,
    ) -> None:
        activation_lock = self._activation_locks.setdefault(delegation_id, asyncio.Lock())
        async with activation_lock:
            prepared = await self._prepare_activation_unlocked(
                delegation_id,
                input_value,
                message_id=message_id,
                reply_target_id=reply_target_id,
            )
            await self._start_prepared_activation_unlocked(delegation_id, prepared)

    async def _prepare_activation(
        self,
        delegation_id: str,
        input_value: JsonObject,
        *,
        message_id: str | None = None,
        reply_target_id: str | None = None,
    ) -> _PreparedActivation:
        activation_lock = self._activation_locks.setdefault(delegation_id, asyncio.Lock())
        async with activation_lock:
            return await self._prepare_activation_unlocked(
                delegation_id,
                input_value,
                message_id=message_id,
                reply_target_id=reply_target_id,
            )

    async def _prepare_activation_unlocked(
        self,
        delegation_id: str,
        input_value: JsonObject,
        *,
        message_id: str | None = None,
        reply_target_id: str | None = None,
    ) -> _PreparedActivation:
        if delegation_id in self._prepared or delegation_id in self._active:
            raise DelegationStateError(
                "delegation.activation_active",
                f"delegation {delegation_id} already has a prepared or live activation",
            )
        snapshot = await self.store.snapshot(delegation_id)
        session_ref = await self._restore_provider_session(snapshot)
        required_features = _map_features(snapshot.request.required_features)
        activation_number = snapshot.activation_count + 1
        invocation_id = f"{delegation_id}:invocation:{activation_number}"
        activation_id = f"{delegation_id}:activation:{activation_number}"
        preparing = await self.store.begin_activation(
            delegation_id,
            invocation_id,
            activation_id,
        )
        request = preparing.request
        policy_context = dict(request.constraints)
        policy_context["delegation"] = cast(
            JsonValue,
            {
                "delegation_id": delegation_id,
                "depth": preparing.ref.depth,
                "child_scope": (
                    preparing.ref.child_scope.scope_id
                    if preparing.ref.child_scope is not None
                    else None
                ),
                "tool_allowlist": list[JsonValue](sorted(request.policy.tool_allowlist)),
                "tool_denylist": list[JsonValue](sorted(request.policy.tool_denylist)),
                "persona": request.policy.persona,
                "policy_snapshot": (
                    preparing.admission.policy_snapshot if preparing.admission is not None else {}
                ),
            },
        )
        invocation_request = InvocationRequest(
            invocation_id=invocation_id,
            capability_id=request.capability_id,
            operation=request.operation,
            input=input_value,
            idempotency_key=f"{request.idempotency_key}:activation:{preparing.activation_count}",
            completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
            session_ref=session_ref,
            required_features=required_features,
            output_schema=request.output_schema,
            policy_context=policy_context,
            model=request.model,
            effort=request.effort,
            owner_id=request.controller.principal_id,
            scope_id=(
                preparing.ref.child_scope.scope_id
                if preparing.ref.child_scope is not None
                else request.scope.scope_id
            ),
            lease_owner=f"delegation:{delegation_id}",
            lease_epoch=preparing.activation_count,
            resource_refs=(f"delegation:{delegation_id}",),
        )
        prepared = _PreparedActivation(
            invocation_id,
            activation_id,
            preparing.activation_count,
            invocation_request,
            message_id,
            reply_target_id,
        )
        self._prepared[delegation_id] = prepared
        return prepared

    async def _start_prepared_activation(self, delegation_id: str) -> None:
        activation_lock = self._activation_locks.setdefault(delegation_id, asyncio.Lock())
        async with activation_lock:
            prepared = self._prepared.get(delegation_id)
            if prepared is None:
                snapshot = await self.store.snapshot(delegation_id)
                if snapshot.status is DelegationStatus.PREPARING:
                    await self.store.finalize(
                        delegation_id,
                        DelegationReport(
                            delegation_id=delegation_id,
                            status=DelegationStatus.RECONCILIATION_REQUIRED,
                            error_code="delegation.prepared_activation_unavailable",
                            error_message=(
                                "prepared activation state cannot be recovered without "
                                "reconciling the external session"
                            ),
                            source_invocation_id=snapshot.current_invocation_id,
                            source_activation_id=snapshot.current_activation_id,
                        ),
                    )
                    self._activation_locks.pop(delegation_id, None)
                    return
                raise DelegationStateError(
                    "delegation.prepared_activation_missing",
                    f"delegation {delegation_id} has no prepared activation",
                )
            await self._start_prepared_activation_unlocked(delegation_id, prepared)

    async def _start_prepared_activation_unlocked(
        self,
        delegation_id: str,
        prepared: _PreparedActivation,
    ) -> None:
        snapshot = await self.store.snapshot(delegation_id)
        if (
            snapshot.status is not DelegationStatus.PREPARING
            or snapshot.current_invocation_id != prepared.invocation_id
            or snapshot.current_activation_id != prepared.activation_id
        ):
            raise DelegationStateError(
                "delegation.prepared_activation_conflict",
                "prepared activation identity no longer matches delegation facts",
            )
        try:
            handle = await self.invocation_runtime.submit(
                prepared.request,
                provider_id=(
                    snapshot.request.provider_id
                    or (
                        prepared.request.session_ref.provider
                        if prepared.request.session_ref is not None
                        else None
                    )
                ),
            )
            active_snapshot = await self.store.mark_activation_active(
                delegation_id,
                prepared.invocation_id,
                prepared.activation_id,
            )
            await self._publish_session_event(
                delegation_id,
                event_id=(
                    f"{delegation_id}:activation:{prepared.activation_number}:started"
                ),
                kind=DelegationSessionEventKind.LIFECYCLE,
                invocation_id=prepared.invocation_id,
                activation_id=prepared.activation_id,
                activation_number=prepared.activation_number,
                status=active_snapshot.status.value,
                payload={
                    "stage": "activation_started",
                    "provider_id": active_snapshot.request.provider_id,
                    "model": active_snapshot.request.model,
                    "effort": active_snapshot.request.effort,
                },
            )
        except Exception as exc:
            self._prepared.pop(delegation_id, None)
            report = DelegationReport(
                delegation_id=delegation_id,
                status=DelegationStatus.FAILED,
                error_code=getattr(exc, "code", type(exc).__name__),
                error_message=str(exc),
                source_invocation_id=prepared.invocation_id,
                source_activation_id=prepared.activation_id,
            )
            await self.store.finalize(
                delegation_id,
                report,
            )
            await self._publish_terminal_report(
                snapshot.request,
                report,
                invocation_id=prepared.invocation_id,
                activation_id=prepared.activation_id,
                activation_number=prepared.activation_number,
            )
            if prepared.message_id is not None:
                await self._complete_message(snapshot, prepared.message_id)
            if prepared.reply_target_id is not None:
                await self._complete_message(snapshot, prepared.reply_target_id)
            raise
        self._prepared.pop(delegation_id, None)
        async with self._lock:
            bridge = asyncio.create_task(
                self._bridge(
                    active_snapshot,
                    handle,
                    prepared.message_id,
                    prepared.reply_target_id,
                ),
                name=(f"delegation-bridge:{delegation_id}:{active_snapshot.activation_count}"),
            )
            self._active[delegation_id] = _ActiveActivation(
                prepared.invocation_id,
                prepared.activation_id,
                prepared.activation_number,
                handle,
                bridge,
                prepared.message_id,
                prepared.reply_target_id,
            )

    async def _finalize_submission_failure(self, delegation_id: str, error: Exception) -> None:
        snapshot = await self.store.snapshot(delegation_id)
        if snapshot.report is not None:
            await self._close_one_shot_channel(snapshot)
            return
        status = (
            DelegationStatus.REJECTED
            if isinstance(error, DelegationCapabilityRejected)
            else DelegationStatus.FAILED
        )
        report = DelegationReport(
            delegation_id=delegation_id,
            status=status,
            error_code=getattr(error, "code", type(error).__name__),
            error_message=str(error),
        )
        try:
            await self.store.finalize(
                delegation_id,
                report,
            )
            await self._publish_terminal_report(snapshot.request, report)
        finally:
            await self._close_one_shot_channel(snapshot)

    async def _close_one_shot_channel(self, snapshot: DelegationSnapshot) -> None:
        if snapshot.request.mode is not DelegationMode.ONE_SHOT or snapshot.ref.channel_id is None:
            return
        try:
            await self.channel_store.close(snapshot.ref.channel_id)
        except Exception:
            pass

    async def _bridge(
        self,
        snapshot: DelegationSnapshot,
        handle: DelegationExecutionHandle,
        message_id: str | None = None,
        reply_target_id: str | None = None,
    ) -> None:
        delegation_id = snapshot.ref.delegation_id
        report: DelegationReport | None = None
        try:
            async for event in handle.events():
                await self._record_session_event(snapshot, event)
                await self._publish_invocation_event(snapshot, event)
                await self._publish_public_session_event(snapshot, event)
            result = await handle.wait()
            report = _report_from_result(
                delegation_id,
                snapshot.current_invocation_id,
                snapshot.current_activation_id,
                result,
            )
            await self.store.finalize(delegation_id, report)
            await self._publish_terminal_report(
                snapshot.request,
                report,
                invocation_id=snapshot.current_invocation_id,
                activation_id=snapshot.current_activation_id,
                activation_number=snapshot.activation_count,
            )
            if (
                message_id is not None
                and report.status is not DelegationStatus.RECONCILIATION_REQUIRED
            ):
                await self._complete_message(snapshot, message_id)
            if (
                reply_target_id is not None
                and report.status is not DelegationStatus.RECONCILIATION_REQUIRED
            ):
                await self._complete_message(snapshot, reply_target_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report = DelegationReport(
                delegation_id=delegation_id,
                status=DelegationStatus.RECONCILIATION_REQUIRED,
                error_code=getattr(exc, "code", type(exc).__name__),
                error_message=str(exc),
                source_invocation_id=snapshot.current_invocation_id,
                source_activation_id=snapshot.current_activation_id,
            )
            try:
                await self.store.finalize(delegation_id, report)
                await self._publish_terminal_report(
                    snapshot.request,
                    report,
                    invocation_id=snapshot.current_invocation_id,
                    activation_id=snapshot.current_activation_id,
                    activation_number=snapshot.activation_count,
                )
            except Exception:
                report = None
        finally:
            if report is not None:
                await self._publish_child_report(snapshot, report)
            await self._close_one_shot_channel(snapshot)
            async with self._lock:
                active = self._active.get(delegation_id)
                if active is not None and active.handle is handle:
                    self._active.pop(delegation_id, None)
            self._activation_locks.pop(delegation_id, None)

    async def _publish_session_event(
        self,
        delegation_id: str,
        *,
        event_id: str,
        kind: DelegationSessionEventKind,
        invocation_id: str | None = None,
        activation_id: str | None = None,
        activation_number: int | None = None,
        status: str | None = None,
        provider_session_id: str | None = None,
        provider_operation_id: str | None = None,
        payload: JsonObject | None = None,
    ) -> None:
        sink = self.session_events
        if sink is None:
            return
        try:
            await sink.publish(
                delegation_id=delegation_id,
                event_id=event_id,
                kind=kind,
                invocation_id=invocation_id,
                activation_id=activation_id,
                activation_number=activation_number,
                status=status,
                provider_session_id=provider_session_id,
                provider_operation_id=provider_operation_id,
                payload=payload,
            )
        except Exception:
            # Session observation must never change the provider execution outcome.
            return

    async def _publish_terminal_report(
        self,
        request: DelegationRequest,
        report: DelegationReport,
        *,
        invocation_id: str | None = None,
        activation_id: str | None = None,
        activation_number: int | None = None,
    ) -> None:
        if invocation_id is None:
            invocation_id = report.source_invocation_id
        if activation_id is None:
            activation_id = report.source_activation_id
        kind = (
            DelegationSessionEventKind.CANCELLED
            if report.status is DelegationStatus.CANCELLED
            else DelegationSessionEventKind.ERROR
            if report.status
            in {
                DelegationStatus.REJECTED,
                DelegationStatus.FAILED,
                DelegationStatus.RECONCILIATION_REQUIRED,
            }
            else DelegationSessionEventKind.TERMINAL
        )
        event_suffix = activation_number if activation_number is not None else "submission"
        payload: JsonObject = {
            "stage": "terminal",
            "output": report.output,
            "error_code": report.error_code,
            "error_message": report.error_message,
        }
        await self._publish_session_event(
            request.delegation_id,
            event_id=f"{request.delegation_id}:activation:{event_suffix}:terminal",
            kind=kind,
            invocation_id=invocation_id,
            activation_id=activation_id,
            activation_number=activation_number,
            status=report.status.value,
            payload=payload,
        )
        if request.mode is DelegationMode.ONE_SHOT and self.session_events is not None:
            try:
                await self.session_events.close_session(
                    request.delegation_id,
                    event_id=f"{request.delegation_id}:session:closed",
                    invocation_id=invocation_id,
                    activation_id=activation_id,
                    activation_number=activation_number,
                    status=report.status.value,
                )
            except Exception:
                return

    async def _publish_public_session_event(
        self,
        snapshot: DelegationSnapshot,
        event: InvocationEvent,
    ) -> None:
        event_type = event.payload.get("type")
        if not isinstance(event_type, str) or not event_type.strip():
            await self._publish_session_event(
                snapshot.ref.delegation_id,
                event_id=(
                    f"{snapshot.ref.delegation_id}:activation:{snapshot.activation_count}:"
                    f"provider:{event.sequence}"
                ),
                kind=DelegationSessionEventKind.LIFECYCLE,
                invocation_id=snapshot.current_invocation_id,
                activation_id=snapshot.current_activation_id,
                activation_number=snapshot.activation_count,
                status=event.status.value,
                payload={"provider_event_type": "progress"},
            )
            return
        kind = _PUBLIC_AGENT_EVENT_KINDS.get(
            event_type.lower(),
            DelegationSessionEventKind.LIFECYCLE,
        )
        await self._publish_session_event(
            snapshot.ref.delegation_id,
            event_id=(
                f"{snapshot.ref.delegation_id}:activation:{snapshot.activation_count}:"
                f"provider:{event.sequence}"
            ),
            kind=kind,
            invocation_id=snapshot.current_invocation_id,
            activation_id=snapshot.current_activation_id,
            activation_number=snapshot.activation_count,
            status=event.status.value,
            provider_session_id=_optional_string(event.payload.get("provider_session_id")),
            provider_operation_id=_optional_string(event.payload.get("provider_operation_id")),
            payload=_public_provider_payload(event_type, event.payload),
        )

    async def _complete_message(self, snapshot: DelegationSnapshot, message_id: str) -> None:
        if snapshot.ref.channel_id is None:
            return
        try:
            current = await self.channel_store.get_message(snapshot.ref.channel_id, message_id)
            if current.delivery_status is MessageDeliveryStatus.ACCEPTED:
                current = await self.channel_store.transition(
                    snapshot.ref.channel_id,
                    message_id,
                    MessageDeliveryStatus.DELIVERED,
                    expected_status=MessageDeliveryStatus.ACCEPTED,
                )
            if current.delivery_status is MessageDeliveryStatus.DELIVERED:
                current = await self.channel_store.transition(
                    snapshot.ref.channel_id,
                    message_id,
                    MessageDeliveryStatus.PROCESSED,
                    expected_status=MessageDeliveryStatus.DELIVERED,
                )
            if current.delivery_status is MessageDeliveryStatus.PROCESSED:
                await self.channel_store.transition(
                    snapshot.ref.channel_id,
                    message_id,
                    MessageDeliveryStatus.COMPLETED,
                    expected_status=MessageDeliveryStatus.PROCESSED,
                )
        except Exception:
            return

    async def _publish_invocation_event(
        self,
        snapshot: DelegationSnapshot,
        event: InvocationEvent,
    ) -> None:
        if snapshot.ref.channel_id is None:
            return
        status = event.status
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
                        f"{snapshot.activation_count}:event:{event.sequence}"
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
                            "invocation_id": snapshot.current_invocation_id,
                            "activation_id": snapshot.current_activation_id,
                            "status": status.value,
                            "payload": event.payload,
                        },
                    ),
                    scope=snapshot.ref.child_scope or snapshot.request.scope,
                    correlation_id=snapshot.ref.delegation_id,
                )
            )
        except Exception:
            # Delivery is an observation path; execution facts remain authoritative.
            return

    async def _restore_provider_session(
        self,
        snapshot: DelegationSnapshot,
    ) -> SessionRef | None:
        if self.session_log is None or snapshot.request.mode is not DelegationMode.CONTINUABLE:
            return None
        session_id = snapshot.ref.session_id
        if session_id is None:
            raise DelegationStateError(
                "delegation.session_missing",
                "continuable delegation has no session identity",
            )
        composition_id = cast(str, self.composition_id)
        expected = SessionHeader(
            session_id=session_id,
            owner_id=snapshot.request.controller.principal_id,
            scope_id=(snapshot.ref.child_scope or snapshot.request.scope).scope_id,
            composition_id=composition_id,
            metadata={"delegation_id": snapshot.ref.delegation_id},
        )
        try:
            header = await self.session_log.get(session_id)
        except DurableNotFound:
            try:
                header = await self.session_log.create(expected)
            except DurableConflict as exc:
                if exc.code != "session.header_conflict":
                    raise
                header = await self.session_log.get(session_id)
        self._validate_session_header(header, expected)

        binding_id = f"delegation:{snapshot.ref.delegation_id}:provider-binding"
        facts = await self.session_log.read(session_id)
        binding = next(
            (fact for fact in facts if fact.event_id == binding_id),
            None,
        )
        if binding is None:
            return None
        if binding.event_type != "delegation.provider_session_bound":
            raise DurableConflict(
                "delegation.provider_binding_type_conflict",
                f"session {session_id} has an invalid provider binding fact",
            )
        delegation_id = _session_fact_string(binding.payload, "delegation_id")
        provider_id = _session_fact_string(binding.payload, "provider_id")
        provider_session_id = _session_fact_string(
            binding.payload,
            "provider_session_id",
        )
        if delegation_id != snapshot.ref.delegation_id:
            raise DurableConflict(
                "delegation.provider_binding_delegation_conflict",
                f"session {session_id} is bound to another delegation",
            )
        if snapshot.request.provider_id is not None and snapshot.request.provider_id != provider_id:
            raise DelegationCapabilityRejected(
                "delegation.provider_session_mismatch",
                "delegation provider does not match its persisted session binding",
            )
        return SessionRef(provider=provider_id, native_id=provider_session_id)

    @staticmethod
    def _validate_session_header(header: SessionHeader, expected: SessionHeader) -> None:
        actual_fields = (
            header.session_id,
            header.owner_id,
            header.scope_id,
            header.composition_id,
            header.metadata,
        )
        expected_fields = (
            expected.session_id,
            expected.owner_id,
            expected.scope_id,
            expected.composition_id,
            expected.metadata,
        )
        if actual_fields != expected_fields:
            raise DurableConflict(
                "delegation.session_header_conflict",
                f"session {expected.session_id} does not match the delegation identity",
            )

    async def _record_session_event(
        self,
        snapshot: DelegationSnapshot,
        event: InvocationEvent,
    ) -> None:
        if self.session_log is None or snapshot.request.mode is not DelegationMode.CONTINUABLE:
            return
        session_id = snapshot.ref.session_id
        if session_id is None:
            raise DelegationStateError(
                "delegation.session_missing",
                "continuable delegation has no session identity",
            )
        provider_session_id = event.payload.get("provider_session_id")
        if provider_session_id is not None:
            if not isinstance(provider_session_id, str) or not provider_session_id.strip():
                raise DurableConflict(
                    "delegation.provider_session_invalid",
                    "invocation event has an invalid provider session id",
                )
            provider_id = event.payload.get("provider_id")
            if not isinstance(provider_id, str) or not provider_id.strip():
                raise DurableConflict(
                    "delegation.provider_binding_incomplete",
                    "provider session facts require a provider id",
                )
            await self.session_log.append(
                session_id,
                f"delegation:{snapshot.ref.delegation_id}:provider-binding",
                "delegation.provider_session_bound",
                {
                    "delegation_id": snapshot.ref.delegation_id,
                    "provider_id": provider_id,
                    "provider_session_id": provider_session_id,
                },
                occurred_at=event.occurred_at,
            )
        await self.session_log.append(
            session_id,
            (
                f"delegation:{snapshot.ref.delegation_id}:activation:"
                f"{snapshot.activation_count}:invocation-event:{event.sequence}"
            ),
            "delegation.invocation_event",
            {
                "delegation_id": snapshot.ref.delegation_id,
                "invocation_id": event.invocation_id,
                "activation_id": snapshot.current_activation_id,
                "activation_number": snapshot.activation_count,
                "sequence": event.sequence,
                "status": event.status.value,
                "payload": event.payload,
            },
            occurred_at=event.occurred_at,
        )

    async def _publish_child_report(
        self, snapshot: DelegationSnapshot, report: DelegationReport
    ) -> None:
        parent_id = snapshot.ref.parent_delegation_id
        if parent_id is None:
            return
        try:
            parent = await self.store.snapshot(parent_id)
        except Exception:
            return
        if parent.ref.channel_id is None:
            return
        try:
            await self.channel_store.publish(
                InteractionMessageDraft(
                    message_id=f"{snapshot.ref.delegation_id}:report:{len(parent.child_refs)}",
                    channel_id=parent.ref.channel_id,
                    sender=PrincipalRef(
                        f"delegation:{snapshot.ref.delegation_id}",
                        PrincipalKind.AGENT,
                    ),
                    recipient=parent.request.controller,
                    message_type=MessageType.RESULT,
                    payload={
                        "delegation_id": snapshot.ref.delegation_id,
                        "status": report.status.value,
                        "output": report.output,
                        "error_code": report.error_code,
                        "error_message": report.error_message,
                        "source_invocation_id": report.source_invocation_id,
                        "source_activation_id": report.source_activation_id,
                    },
                    scope=parent.ref.child_scope or parent.request.scope,
                    correlation_id=parent.ref.delegation_id,
                    causation_id=report.source_activation_id,
                )
            )
        except Exception:
            return

    async def _parent_snapshot(self, request: DelegationRequest) -> DelegationSnapshot | None:
        parent_id = request.parent_delegation_id
        if parent_id is None:
            return None
        try:
            parent = await self.store.snapshot(parent_id)
        except DelegationNotFound as exc:
            raise DelegationStateError(
                "delegation.parent_not_found",
                f"parent delegation {parent_id} was not found",
            ) from exc
        if parent.status not in {
            DelegationStatus.ADMITTED,
            DelegationStatus.PREPARING,
            DelegationStatus.ACTIVE,
            DelegationStatus.WAITING_INPUT,
        }:
            raise DelegationStateError(
                "delegation.parent_not_active",
                f"parent delegation {parent_id} is not allowed to create a child",
            )
        if parent.request.controller.principal_id != request.initiator.principal_id:
            raise DelegationUnauthorized(
                "delegation.parent_controller_required",
                "the child initiator must be the parent controller",
            )
        next_depth = parent.ref.depth + 1
        if next_depth > parent.request.policy.budget.max_depth:
            raise DelegationCapabilityRejected(
                "delegation.depth_exceeded",
                f"delegation depth {next_depth} exceeds the parent budget",
            )
        if len(parent.child_refs) >= parent.request.policy.budget.fan_out_limit:
            raise DelegationCapabilityRejected(
                "delegation.fan_out_exceeded",
                f"parent delegation {parent_id} exceeded its fan-out budget",
            )
        return parent

    def _initial_ref(
        self, request: DelegationRequest, parent: DelegationSnapshot | None
    ) -> DelegationRef:
        session_id = request.session_id
        channel_id = request.channel_id
        if request.mode is DelegationMode.CONTINUABLE:
            session_id = session_id or f"delegation-session:{request.delegation_id}"
            channel_id = channel_id or f"delegation-channel:{request.delegation_id}"
        parent_scope = (
            parent.ref.child_scope
            if parent is not None and parent.ref.child_scope is not None
            else request.scope
        )
        child_scope = request.policy.child_scope or ScopeRef(
            f"delegation-scope:{request.delegation_id}",
            parent_scope_id=parent_scope.scope_id,
        )
        if child_scope.parent_scope_id != parent_scope.scope_id:
            raise DelegationCapabilityRejected(
                "delegation.child_scope_escape",
                "child scope must be nested under the parent scope",
            )
        return DelegationRef(
            delegation_id=request.delegation_id,
            session_id=session_id,
            channel_id=channel_id,
            parent_delegation_id=request.parent_delegation_id,
            depth=parent.ref.depth + 1 if parent is not None else 0,
            child_scope=child_scope,
        )

    def _fallback_ref(self, request: DelegationRequest) -> DelegationRef:
        session_id = request.session_id
        channel_id = request.channel_id
        if request.mode is DelegationMode.CONTINUABLE:
            session_id = session_id or f"delegation-session:{request.delegation_id}"
            channel_id = channel_id or f"delegation-channel:{request.delegation_id}"
        child_scope = request.policy.child_scope or ScopeRef(
            f"delegation-scope:{request.delegation_id}",
            parent_scope_id=request.scope.scope_id,
        )
        return DelegationRef(
            delegation_id=request.delegation_id,
            session_id=session_id,
            channel_id=channel_id,
            parent_delegation_id=request.parent_delegation_id,
            child_scope=child_scope,
        )

    @staticmethod
    def _validate_operation_references(
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
        spec: ContinuationOperationSpec,
    ) -> None:
        if request.session_id is None:
            if spec.requires_session:
                raise DelegationStateError(
                    "delegation.session_mismatch",
                    "continuation session does not match delegation session",
                )
            return
        if snapshot.ref.session_id != request.session_id:
            raise DelegationStateError(
                "delegation.session_mismatch",
                "continuation session does not match delegation session",
            )

    async def _validate_operation_channel(
        self, snapshot: DelegationSnapshot, spec: ContinuationOperationSpec
    ) -> None:
        if spec.requires_channel:
            await self._require_open_channel(snapshot)

    @staticmethod
    def _authorize(snapshot: DelegationSnapshot, actor: PrincipalRef) -> None:
        allowed = {
            snapshot.request.initiator.principal_id,
            snapshot.request.controller.principal_id,
            f"delegation:{snapshot.ref.delegation_id}",
        }
        if actor.principal_id not in allowed:
            raise DelegationUnauthorized(
                "delegation.actor_forbidden",
                "principal "
                f"{actor.principal_id} cannot control delegation {snapshot.ref.delegation_id}",
            )

    @staticmethod
    def _can_transition_message(
        snapshot: DelegationSnapshot,
        message: InteractionMessage,
        actor: PrincipalRef,
    ) -> bool:
        if actor.principal_id in {
            snapshot.request.initiator.principal_id,
            snapshot.request.controller.principal_id,
            f"delegation:{snapshot.ref.delegation_id}",
        }:
            return True
        return message.recipient is not None and message.recipient == actor

    @staticmethod
    def _validate_expected_activation(
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
        spec: ContinuationOperationSpec,
    ) -> None:
        if request.expected_activation_id is None:
            if spec.requires_expected_activation:
                raise DelegationStateError(
                    "delegation.activation_fence_required",
                    f"{request.operation.value} requires expected_activation_id",
                )
            return
        expected = snapshot.current_activation_id
        if expected is None and snapshot.report is not None:
            expected = snapshot.report.source_activation_id
        if expected is None and snapshot.report_history:
            expected = snapshot.report_history[-1].source_activation_id
        if expected != request.expected_activation_id:
            raise DelegationStateError(
                "delegation.activation_conflict",
                "continuation expected a different activation",
            )

    @staticmethod
    def _validate_prepare(snapshot: DelegationSnapshot, request: ContinuationRequest) -> None:
        DelegationRuntime._validate_continuable_session(snapshot, request)
        if snapshot.current_invocation_id is not None:
            raise DelegationStateError(
                "delegation.activation_active",
                "an activation cannot be prepared while another activation is live",
            )
        if snapshot.status is not DelegationStatus.WAITING_INPUT and snapshot.report is None:
            raise DelegationStateError(
                "delegation.activation_not_terminal",
                "prepare requires a completed current activation",
            )
        if snapshot.activation_count >= snapshot.request.policy.budget.max_activations:
            raise DelegationStateError(
                "delegation.activation_budget_exceeded",
                "prepare exceeds the delegation activation budget",
            )

    @staticmethod
    def _validate_start(snapshot: DelegationSnapshot, request: ContinuationRequest) -> None:
        DelegationRuntime._validate_continuable_session(snapshot, request)
        if snapshot.status is not DelegationStatus.PREPARING:
            raise DelegationStateError(
                "delegation.activation_not_prepared",
                f"delegation {snapshot.ref.delegation_id} has no prepared activation",
            )
        if request.expected_activation_id is None:
            raise DelegationStateError(
                "delegation.activation_fence_required",
                "start requires expected_activation_id for the prepared activation",
            )
        if request.input:
            raise DelegationStateError(
                "delegation.start_input_invalid",
                "start cannot replace the input captured by prepare",
            )

    @staticmethod
    def _validate_continuable_session(
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> None:
        if snapshot.request.mode is not DelegationMode.CONTINUABLE:
            raise DelegationStateError(
                "delegation.not_continuable",
                f"delegation {snapshot.ref.delegation_id} does not support continuation",
            )
        if request.session_id is None or snapshot.ref.session_id != request.session_id:
            raise DelegationStateError(
                "delegation.session_mismatch",
                "continuation session does not match delegation session",
            )

    async def _validate_live_control(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
        operation_name: str,
    ) -> None:
        self._validate_continuable_session(snapshot, request)
        expected_status = (
            DelegationStatus.PAUSED if operation_name == "resume" else DelegationStatus.ACTIVE
        )
        if snapshot.status is not expected_status:
            raise DelegationStateError(
                f"delegation.{operation_name}_state_invalid",
                (
                    f"{operation_name} requires delegation status "
                    f"{expected_status.value}, got {snapshot.status.value}"
                ),
            )
        active = self._require_active_activation(snapshot.ref.delegation_id)
        self._require_control_method(active, operation_name)

    async def _validate_ack(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> None:
        self._validate_continuable_session(snapshot, request)
        channel_id = cast(str, snapshot.ref.channel_id)
        target = await self.channel_store.get_message(channel_id, cast(str, request.reply_to))
        if not self._can_transition_message(snapshot, target, request.actor):
            raise DelegationUnauthorized(
                "delegation.ack_forbidden",
                "principal cannot acknowledge the target interaction message",
            )
        if request.correlation_id is not None and target.correlation_id != request.correlation_id:
            raise DelegationStateError(
                "delegation.ack_correlation_mismatch",
                "ack correlation does not match the target message",
            )

    async def _validate_close(
        self,
        snapshot: DelegationSnapshot,
        request: ContinuationRequest,
    ) -> None:
        self._validate_continuable_session(snapshot, request)
        if (
            snapshot.current_invocation_id is not None
            or snapshot.ref.delegation_id in self._active
            or snapshot.ref.delegation_id in self._prepared
        ):
            raise DelegationStateError(
                "delegation.close_activation_live",
                "cancel or finish the current activation before closing the session",
            )

    async def _require_open_channel(self, snapshot: DelegationSnapshot) -> str:
        channel_id = snapshot.ref.channel_id
        if channel_id is None:
            raise DelegationStateError(
                "delegation.channel_missing",
                "continuable delegation has no interaction channel",
            )
        channel = await self.channel_store.snapshot(channel_id)
        if channel.closed:
            raise DelegationStateError(
                "delegation.channel_closed",
                f"interaction channel {channel_id} is closed",
            )
        return channel_id

    @staticmethod
    def _validate_follow_up(snapshot: DelegationSnapshot, request: ContinuationRequest) -> None:
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
        if snapshot.current_invocation_id is not None:
            raise DelegationStateError(
                "delegation.activation_active",
                "follow-up cannot start while the current activation is live",
            )
        if snapshot.status is DelegationStatus.WAITING_INPUT:
            if request.reply_to is None:
                raise DelegationStateError(
                    "delegation.reply_required",
                    "waiting input requires a reply target",
                )
        elif snapshot.report is None:
            raise DelegationStateError(
                "delegation.activation_not_terminal",
                "follow-up requires a completed current activation",
            )
        if snapshot.activation_count >= snapshot.request.policy.budget.max_activations:
            raise DelegationStateError(
                "delegation.activation_budget_exceeded",
                "follow-up exceeds the delegation activation budget",
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

    async def send_message(
        self, actor: PrincipalRef, draft: InteractionMessageDraft
    ) -> InteractionMessage:
        return await self._runtime.send_message(self._delegation_id, actor, draft)

    async def transition_message(
        self,
        actor: PrincipalRef,
        message_id: str,
        status: MessageDeliveryStatus,
        *,
        expected_status: MessageDeliveryStatus | None = None,
    ) -> InteractionMessage:
        return await self._runtime.transition_message(
            self._delegation_id,
            actor,
            message_id,
            status,
            expected_status=expected_status,
        )

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


def _session_fact_string(payload: JsonObject, field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise DurableConflict(
            "delegation.provider_binding_invalid",
            f"provider binding field {field_name} must be a non-empty string",
        )
    return value


def _report_from_result(
    delegation_id: str,
    invocation_id: str | None,
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
        source_invocation_id=invocation_id,
        source_activation_id=activation_id,
    )


def _public_provider_payload(event_type: str, payload: JsonObject) -> JsonObject:
    """Keep the observation contract to public progress fields only."""

    allowed_fields = {
        "text",
        "phase",
        "name",
        "tool",
        "tool_id",
        "tool_name",
        "tool_use_id",
        "command",
        "status",
        "summary",
        "turn_id",
        "item_id",
        "parent_item_id",
        "parent_tool_use_id",
        "agent_id",
        "stream",
        "exit_code",
        "duration_ms",
        "path",
        "change_kind",
        "section_index",
        "error_code",
        "error_message",
        "provider_session_id",
        "provider_operation_id",
    }
    public: JsonObject = {"provider_event_type": event_type}
    for field_name in allowed_fields:
        value = payload.get(field_name)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                public[field_name] = cast(JsonValue, value)
    plan = _public_plan(payload.get("plan"))
    if plan:
        public["plan"] = cast(JsonValue, plan)
    changes = _public_file_changes(payload.get("changes"))
    if changes:
        public["changes"] = cast(JsonValue, changes)
    return public


_PUBLIC_AGENT_EVENT_KINDS: dict[str, DelegationSessionEventKind] = {
    "agent.turn.started": DelegationSessionEventKind.TURN_STARTED,
    "agent.turn.completed": DelegationSessionEventKind.TURN_COMPLETED,
    "agent.message.delta": DelegationSessionEventKind.OUTPUT_DELTA,
    "agent.message.completed": DelegationSessionEventKind.OUTPUT_COMPLETED,
    "agent.reasoning.delta": DelegationSessionEventKind.REASONING_DELTA,
    "agent.reasoning.completed": DelegationSessionEventKind.REASONING_COMPLETED,
    "agent.plan.delta": DelegationSessionEventKind.PLAN_DELTA,
    "agent.plan.completed": DelegationSessionEventKind.PLAN_COMPLETED,
    "agent.tool.started": DelegationSessionEventKind.TOOL_STARTED,
    "agent.tool.output.delta": DelegationSessionEventKind.TOOL_OUTPUT_DELTA,
    "agent.tool.completed": DelegationSessionEventKind.TOOL_COMPLETED,
    "agent.command.started": DelegationSessionEventKind.COMMAND_STARTED,
    "agent.command.output.delta": DelegationSessionEventKind.COMMAND_OUTPUT_DELTA,
    "agent.command.completed": DelegationSessionEventKind.COMMAND_COMPLETED,
    "agent.file.changed": DelegationSessionEventKind.FILE_CHANGED,
    "agent.task.started": DelegationSessionEventKind.TASK_STARTED,
    "agent.task.progress": DelegationSessionEventKind.TASK_PROGRESS,
    "agent.task.completed": DelegationSessionEventKind.TASK_COMPLETED,
}


def _public_plan(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    result: list[JsonObject] = []
    for raw_entry in cast(list[object], value):
        if not isinstance(raw_entry, dict):
            continue
        entry = cast(dict[object, object], raw_entry)
        step = entry.get("step")
        status = entry.get("status")
        if not isinstance(step, str) or not step.strip():
            continue
        public_entry: JsonObject = {"step": step.strip()}
        if isinstance(status, str) and status.strip():
            public_entry["status"] = status.strip()
        result.append(public_entry)
    return result


def _public_file_changes(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    result: list[JsonObject] = []
    for raw_change in cast(list[object], value):
        if not isinstance(raw_change, dict):
            continue
        change = cast(dict[object, object], raw_change)
        path = change.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        public_change: JsonObject = {"path": path.strip()}
        kind = change.get("kind")
        if isinstance(kind, str) and kind.strip():
            public_change["kind"] = kind.strip()
        result.append(public_change)
    return result


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


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
