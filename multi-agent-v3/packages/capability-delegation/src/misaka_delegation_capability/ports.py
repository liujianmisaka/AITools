from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from misaka_delegation_contracts import (
    ContinuationRequest,
    DelegationAdmission,
    DelegationReconciliationResolution,
    DelegationRef,
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
)
from misaka_interaction_contracts import (
    InteractionMessage,
    InteractionMessageDraft,
    MessageCursor,
    MessageDeliveryStatus,
    PrincipalRef,
)
from misaka_invocation_contracts import (
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    ReconcileResult,
)
from misaka_kernel_contracts import ServiceKey

DELEGATION_RUNTIME_SERVICE = ServiceKey("capability.delegation.runtime")


class DelegationHandle(Protocol):
    @property
    def delegation_id(self) -> str: ...

    async def wait(self) -> DelegationReport: ...

    async def snapshot(self) -> DelegationSnapshot: ...

    def messages(
        self, *, cursor: MessageCursor | None = None
    ) -> AsyncIterator[InteractionMessage]: ...

    async def send_message(
        self, actor: PrincipalRef, draft: InteractionMessageDraft
    ) -> InteractionMessage: ...

    async def transition_message(
        self,
        actor: PrincipalRef,
        message_id: str,
        status: MessageDeliveryStatus,
        *,
        expected_status: MessageDeliveryStatus | None = None,
    ) -> InteractionMessage: ...

    async def continue_request(self, request: ContinuationRequest) -> DelegationHandle: ...

    async def cancel(self, actor_id: str, reason: str) -> None: ...


class DelegationRuntimePort(Protocol):
    async def submit(self, request: DelegationRequest) -> DelegationHandle: ...

    async def recover(self) -> tuple[DelegationSnapshot, ...]: ...

    async def continue_request(self, request: ContinuationRequest) -> DelegationHandle: ...

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot: ...

    async def children(self, delegation_id: str) -> tuple[DelegationSnapshot, ...]: ...

    async def read_messages(
        self,
        delegation_id: str,
        *,
        cursor: MessageCursor | None = None,
    ) -> tuple[InteractionMessage, ...]: ...

    async def send_message(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        draft: InteractionMessageDraft,
    ) -> InteractionMessage: ...

    async def transition_message(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        message_id: str,
        status: MessageDeliveryStatus,
        *,
        expected_status: MessageDeliveryStatus | None = None,
    ) -> InteractionMessage: ...

    async def resolve_reconciliation(
        self, resolution: DelegationReconciliationResolution
    ) -> DelegationSnapshot: ...


class DelegationGatewayPort(Protocol):
    """Principal-facing adapter over Delegation and Interaction ports."""

    async def create(
        self, request: DelegationRequest, actor: PrincipalRef
    ) -> DelegationSnapshot: ...

    async def get(self, delegation_id: str, actor: PrincipalRef) -> DelegationSnapshot: ...

    async def children(
        self, delegation_id: str, actor: PrincipalRef
    ) -> tuple[DelegationSnapshot, ...]: ...

    async def send(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        draft: InteractionMessageDraft,
    ) -> InteractionMessage: ...

    async def events(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        *,
        cursor: MessageCursor | None = None,
    ) -> tuple[InteractionMessage, ...]: ...

    async def stream_events(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        *,
        cursor: MessageCursor | None = None,
    ) -> AsyncIterator[InteractionMessage]: ...

    async def reply(self, request: ContinuationRequest) -> DelegationSnapshot: ...

    async def cancel(self, request: ContinuationRequest) -> DelegationSnapshot: ...

    async def reconcile(self, request: ContinuationRequest) -> DelegationSnapshot: ...

    async def resolve_reconciliation(
        self, resolution: DelegationReconciliationResolution
    ) -> DelegationSnapshot: ...


class DelegationExecutionHandle(Protocol):
    invocation_id: str
    activation_id: str

    def events(self) -> AsyncIterator[InvocationEvent]: ...

    async def wait(self) -> InvocationResult: ...

    async def cancel(self, reason: str) -> None: ...

    async def reconcile(self) -> ReconcileResult: ...


class DelegationExecutionPort(Protocol):
    async def submit(
        self, request: InvocationRequest, *, provider_id: str | None = None
    ) -> DelegationExecutionHandle: ...


class DelegationStore(Protocol):
    async def create(
        self, request: DelegationRequest, ref: DelegationRef
    ) -> tuple[DelegationSnapshot, bool]: ...

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot: ...

    async def list(self) -> tuple[DelegationSnapshot, ...]: ...

    async def list_children(self, parent_delegation_id: str) -> tuple[DelegationSnapshot, ...]: ...

    async def bind_ref(self, delegation_id: str, ref: DelegationRef) -> DelegationSnapshot: ...

    async def record_admission(
        self, delegation_id: str, admission: DelegationAdmission
    ) -> DelegationSnapshot: ...

    async def attach_child(
        self, parent_delegation_id: str, child_ref: DelegationRef
    ) -> DelegationSnapshot: ...

    async def mark_waiting_input(
        self, delegation_id: str, message_id: str
    ) -> DelegationSnapshot: ...

    async def claim_continuation(
        self, delegation_id: str, idempotency_key: str, fingerprint: str
    ) -> bool: ...

    async def continuation_fingerprint(
        self, delegation_id: str, idempotency_key: str
    ) -> str | None: ...

    async def begin_activation(
        self,
        delegation_id: str,
        invocation_id: str,
        activation_id: str,
    ) -> DelegationSnapshot: ...

    async def mark_activation_active(
        self, delegation_id: str, invocation_id: str, activation_id: str
    ) -> DelegationSnapshot: ...

    async def mark_activation_paused(
        self, delegation_id: str, invocation_id: str, activation_id: str
    ) -> DelegationSnapshot: ...

    async def mark_activation_resumed(
        self, delegation_id: str, invocation_id: str, activation_id: str
    ) -> DelegationSnapshot: ...

    async def finalize(
        self, delegation_id: str, report: DelegationReport
    ) -> DelegationSnapshot: ...

    async def resolve_reconciliation(
        self,
        resolution: DelegationReconciliationResolution,
        report: DelegationReport,
    ) -> DelegationSnapshot: ...

    async def wait_terminal(self, delegation_id: str) -> DelegationReport: ...
