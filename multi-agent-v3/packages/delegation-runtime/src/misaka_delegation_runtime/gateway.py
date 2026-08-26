from __future__ import annotations

from collections.abc import AsyncIterator

from misaka_delegation_capability import (
    DelegationGatewayPort,
    DelegationRuntimePort,
    DelegationStateError,
    DelegationUnauthorized,
)
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationReconciliationResolution,
    DelegationRequest,
    DelegationSnapshot,
    MessageDispatchRequest,
    MessageDispatchSnapshot,
)
from misaka_interaction_capability import InteractionChannelStore, InteractionError
from misaka_interaction_contracts import (
    InteractionMessage,
    InteractionMessageDraft,
    MessageCursor,
    PrincipalKind,
    PrincipalRef,
)


class RuntimeDelegationGateway(DelegationGatewayPort):
    """Bind a principal-facing gateway to provider-neutral runtime ports."""

    def __init__(
        self,
        runtime: DelegationRuntimePort,
        channel_store: InteractionChannelStore,
    ) -> None:
        self._runtime = runtime
        self._channel_store = channel_store

    async def create(self, request: DelegationRequest, actor: PrincipalRef) -> DelegationSnapshot:
        if _principal_identity(actor) != _principal_identity(request.initiator):
            raise DelegationUnauthorized(
                "delegation.initiator_forbidden",
                "delegation creator must match the declared initiator",
            )
        handle = await self._runtime.submit(request)
        return await handle.snapshot()

    async def get(self, delegation_id: str, actor: PrincipalRef) -> DelegationSnapshot:
        snapshot = await self._runtime.snapshot(delegation_id)
        _authorize_observer(snapshot, actor)
        return snapshot

    async def children(
        self, delegation_id: str, actor: PrincipalRef
    ) -> tuple[DelegationSnapshot, ...]:
        snapshot = await self._runtime.snapshot(delegation_id)
        _authorize_observer(snapshot, actor)
        return await self._runtime.children(delegation_id)

    async def send(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        draft: InteractionMessageDraft,
    ) -> InteractionMessage:
        snapshot = await self._runtime.snapshot(delegation_id)
        _authorize_controller(snapshot, actor)
        try:
            return await self._runtime.send_message(delegation_id, actor, draft)
        except InteractionError as exc:
            raise _interaction_state_error(exc) from exc

    async def dispatch_message(
        self,
        request: MessageDispatchRequest,
    ) -> MessageDispatchSnapshot:
        snapshot = await self._runtime.snapshot(request.delegation_id)
        _authorize_controller(snapshot, request.actor)
        return await self._runtime.dispatch_message(request)

    async def events(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        *,
        cursor: MessageCursor | None = None,
    ) -> tuple[InteractionMessage, ...]:
        snapshot = await self._runtime.snapshot(delegation_id)
        _authorize_observer(snapshot, actor)
        channel_id = snapshot.ref.channel_id
        if channel_id is None:
            return ()
        effective_cursor = cursor or MessageCursor(channel_id)
        if effective_cursor.channel_id != channel_id:
            raise ValueError("delegation event cursor channel does not match delegation")
        try:
            return await self._channel_store.read(channel_id, cursor=effective_cursor)
        except InteractionError as exc:
            raise _interaction_state_error(exc) from exc

    async def stream_events(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        *,
        cursor: MessageCursor | None = None,
    ) -> AsyncIterator[InteractionMessage]:
        snapshot = await self._runtime.snapshot(delegation_id)
        _authorize_observer(snapshot, actor)
        channel_id = snapshot.ref.channel_id
        effective_cursor = (
            cursor
            if cursor is not None
            else MessageCursor(channel_id)
            if channel_id is not None
            else None
        )
        if effective_cursor is not None and effective_cursor.channel_id != channel_id:
            raise ValueError("delegation event cursor channel does not match delegation")

        async def _stream() -> AsyncIterator[InteractionMessage]:
            if channel_id is None or effective_cursor is None:
                return
            try:
                async for message in self._channel_store.events(
                    channel_id,
                    cursor=effective_cursor,
                ):
                    yield message
            except InteractionError as exc:
                raise _interaction_state_error(exc) from exc

        return _stream()

    async def reply(self, request: ContinuationRequest) -> DelegationSnapshot:
        return await self._continue(request, ContinuationOperation.REPLY)

    async def cancel(self, request: ContinuationRequest) -> DelegationSnapshot:
        return await self._continue(request, ContinuationOperation.CANCEL)

    async def reconcile(self, request: ContinuationRequest) -> DelegationSnapshot:
        return await self._continue(request, ContinuationOperation.RECONCILE)

    async def resolve_reconciliation(
        self,
        resolution: DelegationReconciliationResolution,
    ) -> DelegationSnapshot:
        snapshot = await self._runtime.snapshot(resolution.delegation_id)
        _authorize_controller(snapshot, resolution.actor)
        return await self._runtime.resolve_reconciliation(resolution)

    async def _continue(
        self,
        request: ContinuationRequest,
        expected_operation: ContinuationOperation,
    ) -> DelegationSnapshot:
        if request.operation is not expected_operation:
            raise ValueError(
                f"gateway {expected_operation.value} requires a matching continuation operation"
            )
        snapshot = await self._runtime.snapshot(request.delegation_id)
        _authorize_controller(snapshot, request.actor)
        try:
            handle = await self._runtime.continue_request(request)
        except InteractionError as exc:
            raise _interaction_state_error(exc) from exc
        return await handle.snapshot()


def _authorize_observer(snapshot: DelegationSnapshot, actor: PrincipalRef) -> None:
    allowed = {
        _principal_identity(snapshot.request.initiator),
        _principal_identity(snapshot.request.controller),
        *(_principal_identity(observer) for observer in snapshot.request.observers),
        _principal_identity(_child_principal(snapshot)),
    }
    if _principal_identity(actor) not in allowed:
        raise DelegationUnauthorized(
            "delegation.observer_forbidden",
            "principal "
            f"{actor.principal_id} cannot observe delegation {snapshot.ref.delegation_id}",
        )


def _authorize_controller(snapshot: DelegationSnapshot, actor: PrincipalRef) -> None:
    allowed = {
        _principal_identity(snapshot.request.initiator),
        _principal_identity(snapshot.request.controller),
        _principal_identity(_child_principal(snapshot)),
    }
    if _principal_identity(actor) not in allowed:
        raise DelegationUnauthorized(
            "delegation.controller_forbidden",
            "principal "
            f"{actor.principal_id} cannot control delegation {snapshot.ref.delegation_id}",
        )


def _child_principal(snapshot: DelegationSnapshot) -> PrincipalRef:
    return PrincipalRef(
        f"delegation:{snapshot.ref.delegation_id}",
        PrincipalKind.AGENT,
    )


def _principal_identity(principal: PrincipalRef) -> tuple[str, PrincipalKind]:
    return principal.principal_id, principal.kind


def _interaction_state_error(error: InteractionError) -> DelegationStateError:
    return DelegationStateError(
        f"delegation.{error.code}",
        str(error),
    )
