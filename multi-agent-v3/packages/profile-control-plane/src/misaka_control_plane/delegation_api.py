from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
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
    DelegationReport,
    DelegationSnapshot,
)
from misaka_interaction_contracts import (
    InteractionMessage,
    InteractionMessageDraft,
    MessageCursor,
    PrincipalKind,
    PrincipalRef,
)

from misaka_control_plane.delegation_gateway_policy import delegation_continuation_input
from misaka_control_plane.models import (
    DecisionView,
    DelegationApprovalSubmission,
    DelegationCancelSubmission,
    DelegationMessageSubmission,
    DelegationReconcileSubmission,
    DelegationReplySubmission,
    DelegationReportView,
    DelegationSubmission,
    DelegationView,
    InteractionMessageView,
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
            snapshot = await service.delegation(delegation_id, actor)
            request = ContinuationRequest(
                request_id=submission.request_id,
                delegation_id=delegation_id,
                operation=ContinuationOperation.REPLY,
                actor=actor,
                idempotency_key=submission.idempotency_key,
                session_id=submission.session_id,
                message_id=submission.message_id,
                expected_activation_id=submission.expected_activation_id,
                input=delegation_continuation_input(snapshot, submission.input),
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
        created_at=report.created_at.isoformat(),
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


def _delegation_http_error(error: Exception) -> HTTPException:
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
