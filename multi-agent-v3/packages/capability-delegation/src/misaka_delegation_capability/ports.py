from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from misaka_delegation_contracts import (
    ContinuationRequest,
    DelegationRef,
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
)
from misaka_interaction_contracts import InteractionMessage, MessageCursor
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

    async def continue_request(self, request: ContinuationRequest) -> DelegationHandle: ...

    async def cancel(self, actor_id: str, reason: str) -> None: ...


class DelegationRuntimePort(Protocol):
    async def submit(self, request: DelegationRequest) -> DelegationHandle: ...

    async def continue_request(self, request: ContinuationRequest) -> DelegationHandle: ...

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot: ...


class DelegationStore(Protocol):
    async def create(
        self, request: DelegationRequest, ref: DelegationRef
    ) -> tuple[DelegationSnapshot, bool]: ...

    async def snapshot(self, delegation_id: str) -> DelegationSnapshot: ...

    async def bind_ref(self, delegation_id: str, ref: DelegationRef) -> DelegationSnapshot: ...

    async def claim_continuation(
        self, delegation_id: str, idempotency_key: str, fingerprint: str
    ) -> bool: ...

    async def activate(
        self,
        delegation_id: str,
        invocation_id: str,
        activation_id: str,
    ) -> DelegationSnapshot: ...

    async def finalize(
        self, delegation_id: str, report: DelegationReport
    ) -> DelegationSnapshot: ...

    async def wait_terminal(self, delegation_id: str) -> DelegationReport: ...
