from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
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
    ContinuationRequest,
    DelegationAdmission,
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
    ScopeRef,
)
from misaka_invocation_contracts import (
    CapabilityFeature,
    CompletionBoundary,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
)
from misaka_kernel_contracts import JsonObject, JsonValue

from misaka_delegation_runtime.store import MemoryDelegationStore


@dataclass(slots=True)
class _ActiveActivation:
    invocation_id: str
    activation_id: str
    activation_number: int
    handle: DelegationExecutionHandle
    bridge: asyncio.Task[None]


class DelegationRuntime(DelegationRuntimePort):
    """Compose Delegation, Interaction Channel and Invocation Runtime locally."""

    def __init__(
        self,
        invocation_runtime: DelegationExecutionPort,
        channel_store: InteractionChannelStore,
        *,
        store: DelegationStore | None = None,
        gate: DelegationGate | None = None,
    ) -> None:
        self.invocation_runtime = invocation_runtime
        self.channel_store = channel_store
        self.store: DelegationStore = store or MemoryDelegationStore()
        self.gate = gate or AllowAllDelegationGate()
        self._active: dict[str, _ActiveActivation] = {}
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
        if parent_error is not None:
            admission = DelegationAdmission(
                allowed=False,
                reason=str(parent_error),
                error_code=getattr(parent_error, "code", type(parent_error).__name__),
            )
            await self.store.record_admission(request.delegation_id, admission)
            await self.store.finalize(
                request.delegation_id,
                DelegationReport(
                    delegation_id=request.delegation_id,
                    status=DelegationStatus.REJECTED,
                    error_code=admission.error_code,
                    error_message=admission.reason,
                ),
            )
            return _DelegationHandle(self, request.delegation_id)
        try:
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
                return _DelegationHandle(self, request.delegation_id)
            if parent is not None:
                await self.store.attach_child(parent.ref.delegation_id, ref)
        except Exception as exc:
            await self._finalize_submission_failure(request.delegation_id, exc)
            return _DelegationHandle(self, request.delegation_id)
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
        self._validate_expected_activation(snapshot, request)
        if request.operation is ContinuationOperation.FOLLOW_UP:
            self._validate_follow_up(snapshot, request)
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
        if snapshot.current_invocation_id is not None:
            raise DelegationStateError(
                "delegation.activation_active",
                "follow-up cannot start while the current activation is live",
            )
        if snapshot.report is None:
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
        activation_lock = self._activation_locks.setdefault(delegation_id, asyncio.Lock())
        async with activation_lock:
            snapshot = await self.store.snapshot(delegation_id)
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
                        preparing.admission.policy_snapshot
                        if preparing.admission is not None
                        else {}
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
                required_features=required_features,
                output_schema=request.output_schema,
                policy_context=policy_context,
                model=request.model,
                effort=request.effort,
            )
            try:
                handle = await self.invocation_runtime.submit(
                    invocation_request,
                    provider_id=request.provider_id,
                )
                active_snapshot = await self.store.mark_activation_active(
                    delegation_id,
                    invocation_id,
                    activation_id,
                )
            except Exception as exc:
                await self.store.finalize(
                    delegation_id,
                    DelegationReport(
                        delegation_id=delegation_id,
                        status=DelegationStatus.FAILED,
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=str(exc),
                        source_invocation_id=invocation_id,
                        source_activation_id=activation_id,
                    ),
                )
                raise
            async with self._lock:
                bridge = asyncio.create_task(
                    self._bridge(active_snapshot, handle),
                    name=f"delegation-bridge:{delegation_id}:{active_snapshot.activation_count}",
                )
                active = _ActiveActivation(
                    invocation_id,
                    activation_id,
                    active_snapshot.activation_count,
                    handle,
                    bridge,
                )
                self._active[delegation_id] = active

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
        try:
            await self.store.finalize(
                delegation_id,
                DelegationReport(
                    delegation_id=delegation_id,
                    status=status,
                    error_code=getattr(error, "code", type(error).__name__),
                    error_message=str(error),
                ),
            )
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
    ) -> None:
        delegation_id = snapshot.ref.delegation_id
        report: DelegationReport | None = None
        try:
            async for event in handle.events():
                await self._publish_invocation_event(snapshot, event)
            result = await handle.wait()
            report = _report_from_result(
                delegation_id,
                snapshot.current_invocation_id,
                snapshot.current_activation_id,
                result,
            )
            await self.store.finalize(delegation_id, report)
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
                            "invocation_id": snapshot.current_invocation_id,
                            "activation_id": snapshot.current_activation_id,
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
                    scope=parent.request.scope,
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

    @staticmethod
    def _validate_expected_activation(
        snapshot: DelegationSnapshot, request: ContinuationRequest
    ) -> None:
        if request.expected_activation_id is None:
            return
        expected = snapshot.current_activation_id
        if expected is None and snapshot.report is not None:
            expected = snapshot.report.source_activation_id
        if expected != request.expected_activation_id:
            raise DelegationStateError(
                "delegation.activation_conflict",
                "continuation expected a different activation",
            )

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
        if snapshot.report is None:
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
