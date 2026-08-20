from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from misaka_delegation_contracts import (
    ContinuationRequest,
    DelegationAdmission,
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

    async def continue_request(self, request: ContinuationRequest) -> DelegationHandle: ...

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot: ...

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

    async def finalize(
        self, delegation_id: str, report: DelegationReport
    ) -> DelegationSnapshot: ...

    async def wait_terminal(self, delegation_id: str) -> DelegationReport: ...
