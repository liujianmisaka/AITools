from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from misaka_approval_capability import DecisionError, DecisionNotFound
from misaka_delegation_capability import (
    DelegationCapabilityRejected,
    DelegationConflict,
    DelegationNotFound,
    DelegationStateError,
    DelegationUnauthorized,
)
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationReconciliationResolution,
    DelegationReport,
    DelegationSnapshot,
    DelegationStatus,
    MessageDispatchRequest,
    MessageDispatchSnapshot,
)
from misaka_delegation_runtime import (
    DelegationSessionEvent,
    DelegationSessionEventInspection,
)
from misaka_interaction_contracts import (
    InteractionMessage,
    InteractionMessageDraft,
    MessageCursor,
    PrincipalKind,
    PrincipalRef,
)

from misaka_control_plane.models import (
    DecisionView,
    DelegationApprovalSubmission,
    DelegationCancelSubmission,
    DelegationMessageDispatchSubmission,
    DelegationMessageSubmission,
    DelegationReconcileSubmission,
    DelegationReconciliationResolutionSubmission,
    DelegationReplySubmission,
    DelegationReportView,
    DelegationSessionEventView,
    DelegationSessionView,
    DelegationSubmission,
    DelegationTriggerSubmission,
    DelegationView,
    InteractionMessageView,
    MessageDispatchView,
    PrincipalSubmission,
    ScopeSubmission,
)
from misaka_control_plane.service import ControlPlaneService


def create_delegation_router(service: ControlPlaneService) -> APIRouter:
    router = APIRouter(prefix="/delegations", tags=["delegations"])

    @router.post("", response_model=DelegationView, status_code=202)
    async def create_delegation(  # pyright: ignore[reportUnusedFunction]
        submission: DelegationSubmission,
    ) -> DelegationView:
        try:
            return _delegation_view(await service.submit_delegation(submission))
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.post("/trigger", response_model=DelegationView, status_code=202)
    async def trigger_delegation(  # pyright: ignore[reportUnusedFunction]
        submission: DelegationTriggerSubmission,
    ) -> DelegationView:
        try:
            return _delegation_view(await service.trigger_delegation(submission))
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.get("", response_model=list[DelegationView])
    async def list_delegations(  # pyright: ignore[reportUnusedFunction]
        actor_kind: PrincipalKind,
        actor_id: str = Query(min_length=1),
    ) -> list[DelegationView]:
        try:
            actor = PrincipalRef(actor_id, actor_kind)
            return [_delegation_view(snapshot) for snapshot in await service.delegations(actor)]
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.get("/{delegation_id}", response_model=DelegationView)
    async def get_delegation(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        actor_kind: PrincipalKind,
        actor_id: str = Query(min_length=1),
    ) -> DelegationView:
        try:
            actor = PrincipalRef(actor_id, actor_kind)
            return _delegation_view(await service.delegation(delegation_id, actor))
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.get("/{delegation_id}/children", response_model=list[DelegationView])
    async def get_delegation_children(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        actor_kind: PrincipalKind,
        actor_id: str = Query(min_length=1),
    ) -> list[DelegationView]:
        try:
            actor = PrincipalRef(actor_id, actor_kind)
            return [
                _delegation_view(snapshot)
                for snapshot in await service.delegation_children(delegation_id, actor)
            ]
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.post("/{delegation_id}/approve", response_model=DecisionView)
    async def approve_delegation(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        submission: DelegationApprovalSubmission,
    ) -> DecisionView:
        try:
            return DecisionView.from_record(
                await service.approve_delegation(delegation_id, submission)
            )
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.post(
        "/{delegation_id}/messages",
        response_model=InteractionMessageView,
        status_code=202,
    )
    async def send_delegation_message(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        submission: DelegationMessageSubmission,
    ) -> InteractionMessageView:
        try:
            actor = _principal(submission.actor)
            snapshot = await service.delegation(delegation_id, actor)
            channel_id = snapshot.ref.channel_id
            if channel_id is None:
                raise DelegationStateError(
                    "delegation.channel_missing",
                    "delegation has no interaction channel",
                )
            message = await service.send_delegation_message(
                delegation_id,
                actor,
                InteractionMessageDraft(
                    message_id=submission.message_id,
                    channel_id=channel_id,
                    sender=actor,
                    recipient=(
                        _principal(submission.recipient)
                        if submission.recipient is not None
                        else None
                    ),
                    message_type=submission.message_type,
                    payload=submission.payload,
                    payload_schema=submission.payload_schema,
                    scope=snapshot.ref.child_scope or snapshot.request.scope,
                    correlation_id=submission.correlation_id,
                    causation_id=submission.causation_id,
                    reply_to=submission.reply_to,
                ),
            )
            return _interaction_message_view(message)
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.post(
        "/{delegation_id}/messages/dispatch",
        response_model=MessageDispatchView,
        status_code=202,
    )
    async def dispatch_delegation_message(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        submission: DelegationMessageDispatchSubmission,
    ) -> MessageDispatchView:
        try:
            request = MessageDispatchRequest(
                dispatch_id=submission.dispatch_id,
                delegation_id=delegation_id,
                idempotency_key=submission.idempotency_key,
                message_id=submission.message_id,
                actor=_principal(submission.actor),
                session_id=submission.session_id,
                expected_activation_id=submission.expected_activation_id,
                delivery=submission.delivery,
                message_type=submission.message_type,
                payload=submission.payload,
                recipient=(
                    _principal(submission.recipient) if submission.recipient is not None else None
                ),
                correlation_id=submission.correlation_id,
                causation_id=submission.causation_id,
                reply_to=submission.reply_to,
                model=submission.model,
                effort=submission.effort,
            )
            return _message_dispatch_view(await service.dispatch_delegation_message(request))
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.get(
        "/{delegation_id}/events",
        response_model=list[InteractionMessageView],
    )
    async def delegation_events(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        actor_kind: PrincipalKind,
        actor_id: str = Query(min_length=1),
        next_sequence: int = Query(default=1, ge=1),
    ) -> list[InteractionMessageView]:
        try:
            actor = PrincipalRef(actor_id, actor_kind)
            snapshot = await service.delegation(delegation_id, actor)
            cursor = (
                MessageCursor(snapshot.ref.channel_id, next_sequence)
                if snapshot.ref.channel_id is not None
                else None
            )
            return [
                _interaction_message_view(message)
                for message in await service.delegation_events(
                    delegation_id,
                    actor,
                    cursor=cursor,
                )
            ]
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.get(
        "/{delegation_id}/events/stream",
        response_class=StreamingResponse,
    )
    async def delegation_event_stream(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        delegation_id: str,
        actor_kind: PrincipalKind,
        actor_id: str = Query(min_length=1),
        next_sequence: int = Query(default=1, ge=1),
    ) -> StreamingResponse:
        try:
            actor = PrincipalRef(actor_id, actor_kind)
            snapshot = await service.delegation(delegation_id, actor)
            start_sequence = _stream_start_sequence(request, next_sequence)
            cursor = (
                MessageCursor(snapshot.ref.channel_id, start_sequence)
                if snapshot.ref.channel_id is not None
                else None
            )
            stream = await service.delegation_event_stream(
                delegation_id,
                actor,
                cursor=cursor,
            )
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

        async def body() -> AsyncIterator[str]:
            last_sequence = start_sequence - 1
            yield "retry: 3000\n\n"
            yield _sse_event(
                "delegation.ready",
                {"delegation_id": delegation_id, "next_sequence": start_sequence},
            )
            async for message in stream:
                if await request.is_disconnected():
                    return
                last_sequence = message.sequence
                yield _sse_event(
                    "delegation.message",
                    _interaction_message_view(message).model_dump(mode="json"),
                    event_id=str(message.sequence),
                )
                try:
                    snapshot = await service.delegation(delegation_id, actor)
                except Exception:
                    continue
                yield _sse_event(
                    "delegation.snapshot",
                    _delegation_view(snapshot).model_dump(mode="json"),
                )

            if await request.is_disconnected():
                return
            try:
                snapshot = await service.delegation(delegation_id, actor)
            except Exception:
                snapshot = None
            if snapshot is not None:
                yield _sse_event(
                    "delegation.snapshot",
                    _delegation_view(snapshot).model_dump(mode="json"),
                )
            yield _sse_event(
                "delegation.end",
                {"delegation_id": delegation_id, "next_sequence": last_sequence + 1},
            )

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get(
        "/{delegation_id}/session",
        response_model=DelegationSessionView,
    )
    async def delegation_session(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        actor_kind: PrincipalKind,
        actor_id: str = Query(min_length=1),
    ) -> DelegationSessionView:
        try:
            actor = PrincipalRef(actor_id, actor_kind)
            snapshot, inspection = await service.delegation_session(delegation_id, actor)
            latest = await service.delegation_session_events(
                delegation_id,
                actor,
                start_sequence=max(1, inspection.last_sequence),
            )
            return _delegation_session_view(
                snapshot,
                inspection,
                latest[-1] if latest else None,
            )
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.get(
        "/{delegation_id}/session/events",
        response_model=list[DelegationSessionEventView],
    )
    async def delegation_session_events(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        actor_kind: PrincipalKind,
        actor_id: str = Query(min_length=1),
        next_sequence: int = Query(default=1, ge=1),
    ) -> list[DelegationSessionEventView]:
        try:
            actor = PrincipalRef(actor_id, actor_kind)
            return [
                _delegation_session_event_view(event)
                for event in await service.delegation_session_events(
                    delegation_id,
                    actor,
                    start_sequence=next_sequence,
                )
            ]
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.get(
        "/{delegation_id}/session/stream",
        response_class=StreamingResponse,
    )
    async def delegation_session_event_stream(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        delegation_id: str,
        actor_kind: PrincipalKind,
        actor_id: str = Query(min_length=1),
        next_sequence: int = Query(default=1, ge=1),
    ) -> StreamingResponse:
        try:
            actor = PrincipalRef(actor_id, actor_kind)
            snapshot, inspection = await service.delegation_session(delegation_id, actor)
            start_sequence = _stream_start_sequence(request, next_sequence)
            stream = await service.delegation_session_event_stream(
                delegation_id,
                actor,
                start_sequence=start_sequence,
            )
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

        async def body() -> AsyncIterator[str]:
            yield "retry: 3000\n\n"
            yield _sse_event(
                "delegation.session.snapshot",
                _delegation_session_view(snapshot, inspection).model_dump(mode="json"),
            )
            if (inspection.closed and start_sequence > inspection.last_sequence) or (
                snapshot.request.mode.value == "one_shot"
                and snapshot.report is not None
                and inspection.last_sequence == 0
            ):
                yield _sse_event(
                    "delegation.session.end",
                    {"delegation_id": delegation_id, "next_sequence": inspection.last_sequence + 1},
                )
                return
            last_sequence = start_sequence - 1
            async for event in stream:
                if await request.is_disconnected():
                    return
                last_sequence = event.sequence
                yield _sse_event(
                    "delegation.session.event",
                    _delegation_session_event_view(event).model_dump(mode="json"),
                    event_id=str(event.sequence),
                )
                try:
                    current, current_inspection = await service.delegation_session(
                        delegation_id,
                        actor,
                    )
                    yield _sse_event(
                        "delegation.session.snapshot",
                        _delegation_session_view(
                            current,
                            current_inspection,
                            event,
                        ).model_dump(mode="json"),
                    )
                except Exception:
                    continue
            if await request.is_disconnected():
                return
            try:
                current, current_inspection = await service.delegation_session(
                    delegation_id,
                    actor,
                )
                yield _sse_event(
                    "delegation.session.snapshot",
                    _delegation_session_view(current, current_inspection).model_dump(mode="json"),
                )
            except Exception:
                current_inspection = inspection
            yield _sse_event(
                "delegation.session.end",
                {"delegation_id": delegation_id, "next_sequence": last_sequence + 1},
            )

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post(
        "/{delegation_id}/reply",
        response_model=DelegationView,
        status_code=202,
    )
    async def reply_delegation(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        submission: DelegationReplySubmission,
    ) -> DelegationView:
        try:
            actor = _principal(submission.actor)
            request = ContinuationRequest(
                request_id=submission.request_id,
                delegation_id=delegation_id,
                operation=ContinuationOperation.REPLY,
                actor=actor,
                idempotency_key=submission.idempotency_key,
                session_id=submission.session_id,
                message_id=submission.message_id,
                expected_activation_id=submission.expected_activation_id,
                input=submission.input,
                correlation_id=submission.correlation_id,
                reply_to=submission.reply_to,
            )
            return _delegation_view(await service.reply_delegation(request))
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.post(
        "/{delegation_id}/cancel",
        response_model=DelegationView,
        status_code=202,
    )
    async def cancel_delegation(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        submission: DelegationCancelSubmission,
    ) -> DelegationView:
        try:
            request = ContinuationRequest(
                request_id=submission.request_id,
                delegation_id=delegation_id,
                operation=ContinuationOperation.CANCEL,
                actor=_principal(submission.actor),
                idempotency_key=submission.idempotency_key,
                session_id=submission.session_id,
                expected_activation_id=submission.expected_activation_id,
                input={"reason": submission.reason},
            )
            return _delegation_view(await service.cancel_delegation(request))
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.post(
        "/{delegation_id}/reconcile",
        response_model=DelegationView,
    )
    async def reconcile_delegation(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        submission: DelegationReconcileSubmission,
    ) -> DelegationView:
        try:
            request = ContinuationRequest(
                request_id=submission.request_id,
                delegation_id=delegation_id,
                operation=ContinuationOperation.RECONCILE,
                actor=_principal(submission.actor),
                idempotency_key=submission.idempotency_key,
                session_id=submission.session_id,
                expected_activation_id=submission.expected_activation_id,
            )
            return _delegation_view(await service.reconcile_delegation(request))
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    @router.post(
        "/{delegation_id}/reconciliation/resolve",
        response_model=DelegationView,
    )
    async def resolve_delegation_reconciliation(  # pyright: ignore[reportUnusedFunction]
        delegation_id: str,
        submission: DelegationReconciliationResolutionSubmission,
    ) -> DelegationView:
        try:
            resolution = DelegationReconciliationResolution(
                request_id=submission.request_id,
                delegation_id=delegation_id,
                actor=_principal(submission.actor),
                idempotency_key=submission.idempotency_key,
                expected_revision=submission.expected_revision,
                status=DelegationStatus(submission.status),
                reason=submission.reason,
                output=submission.output,
            )
            return _delegation_view(await service.resolve_delegation_reconciliation(resolution))
        except Exception as exc:
            raise _delegation_http_error(exc) from exc

    return router


def _principal(value: PrincipalSubmission) -> PrincipalRef:
    return PrincipalRef(value.principal_id, value.kind, value.display_name)


def _delegation_view(snapshot: DelegationSnapshot) -> DelegationView:
    return DelegationView(
        delegation_id=snapshot.ref.delegation_id,
        status=snapshot.status.value,
        revision=snapshot.revision,
        session_id=snapshot.ref.session_id,
        channel_id=snapshot.ref.channel_id,
        parent_delegation_id=snapshot.ref.parent_delegation_id,
        depth=snapshot.ref.depth,
        child_scope=(
            ScopeSubmission(
                scope_id=snapshot.ref.child_scope.scope_id,
                parent_scope_id=snapshot.ref.child_scope.parent_scope_id,
            )
            if snapshot.ref.child_scope is not None
            else None
        ),
        current_invocation_id=snapshot.current_invocation_id,
        current_activation_id=snapshot.current_activation_id,
        activation_count=snapshot.activation_count,
        child_delegation_ids=[child.delegation_id for child in snapshot.child_refs],
        report=_delegation_report_view(snapshot.report) if snapshot.report else None,
    )


def _delegation_report_view(report: DelegationReport) -> DelegationReportView:
    return DelegationReportView(
        status=report.status.value,
        output=report.output,
        artifact_ids=list(report.artifact_ids),
        error_code=report.error_code,
        error_message=report.error_message,
        source_invocation_id=report.source_invocation_id,
        source_activation_id=report.source_activation_id,
        resolution_reason=report.resolution_reason,
        resolved_by=(
            PrincipalSubmission(
                principal_id=report.resolved_by.principal_id,
                kind=report.resolved_by.kind,
                display_name=report.resolved_by.display_name,
            )
            if report.resolved_by is not None
            else None
        ),
        created_at=report.created_at.isoformat(),
    )


def _message_dispatch_view(snapshot: MessageDispatchSnapshot) -> MessageDispatchView:
    return MessageDispatchView(
        dispatch_id=snapshot.request.dispatch_id,
        delegation_id=snapshot.request.delegation_id,
        session_id=snapshot.request.session_id,
        status=snapshot.status.value,
        revision=snapshot.revision,
        applied_strategy=(
            snapshot.applied_strategy.value if snapshot.applied_strategy is not None else None
        ),
        previous_activation_id=snapshot.previous_activation_id,
        current_activation_id=snapshot.current_activation_id,
        error_code=snapshot.error_code,
        error_message=snapshot.error_message,
        updated_at=(snapshot.updated_at.isoformat() if snapshot.updated_at is not None else None),
    )


def _interaction_message_view(message: InteractionMessage) -> InteractionMessageView:
    return InteractionMessageView(
        message_id=message.message_id,
        channel_id=message.channel_id,
        sender=PrincipalSubmission(
            principal_id=message.sender.principal_id,
            kind=message.sender.kind,
            display_name=message.sender.display_name,
        ),
        recipient=(
            PrincipalSubmission(
                principal_id=message.recipient.principal_id,
                kind=message.recipient.kind,
                display_name=message.recipient.display_name,
            )
            if message.recipient is not None
            else None
        ),
        message_type=message.message_type.value,
        payload=message.payload,
        sequence=message.sequence,
        scope=ScopeSubmission(
            scope_id=message.scope.scope_id,
            parent_scope_id=message.scope.parent_scope_id,
        ),
        correlation_id=message.correlation_id,
        causation_id=message.causation_id,
        reply_to=message.reply_to,
        delivery_status=message.delivery_status.value,
        created_at=message.created_at.isoformat(),
    )


def _delegation_session_event_view(event: DelegationSessionEvent) -> DelegationSessionEventView:
    return DelegationSessionEventView(
        delegation_id=event.delegation_id,
        sequence=event.sequence,
        kind=event.kind.value,
        invocation_id=event.invocation_id,
        activation_id=event.activation_id,
        activation_number=event.activation_number,
        status=event.status,
        provider_session_id=event.provider_session_id,
        provider_operation_id=event.provider_operation_id,
        payload=event.payload,
        occurred_at=event.occurred_at.isoformat(),
    )


def _delegation_session_view(
    snapshot: DelegationSnapshot,
    inspection: DelegationSessionEventInspection,
    latest: DelegationSessionEvent | None = None,
) -> DelegationSessionView:
    stage = None
    if latest is not None:
        raw_stage = latest.payload.get("stage")
        stage = raw_stage if isinstance(raw_stage, str) else latest.kind.value
    return DelegationSessionView(
        delegation=_delegation_view(snapshot),
        provider_id=snapshot.request.provider_id,
        model=snapshot.request.model,
        effort=snapshot.request.effort,
        provider_session_id=inspection.provider_session_id,
        provider_operation_id=inspection.provider_operation_id,
        activation_number=snapshot.activation_count,
        last_sequence=inspection.last_sequence,
        stage=stage or snapshot.status.value,
        closed=inspection.closed,
        updated_at=(
            inspection.last_occurred_at.isoformat()
            if inspection.last_occurred_at is not None
            else None
        ),
    )


def _stream_start_sequence(request: Request, next_sequence: int) -> int:
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is None:
        return next_sequence
    try:
        parsed = int(last_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
    if parsed < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID must not be negative")
    return max(next_sequence, parsed + 1)


def _sse_event(
    event: str,
    data: object,
    *,
    event_id: str | None = None,
) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines.extend(f"data: {line}" for line in payload.splitlines() or [""])
    return "\n".join(lines) + "\n\n"


def _delegation_http_error(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, DelegationUnauthorized):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, DelegationNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, DecisionNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(
        error,
        (DelegationCapabilityRejected, DelegationConflict, DelegationStateError, ValueError),
    ):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, DecisionError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=500, detail=str(error))
