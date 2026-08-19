from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from misaka_invocation_contracts import (
    CapabilityDescriptor,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    ModelDescriptor,
    ProviderExecutionRef,
    ReconcileResult,
)


class ProviderHandle(Protocol):
    def events(self) -> AsyncIterator[InvocationEvent]: ...

    async def wait(self) -> InvocationResult: ...

    async def cancel(self, reason: str) -> None: ...

    async def reconcile(self) -> ReconcileResult: ...

    async def close(self) -> None: ...


class PreparedProviderSession(Protocol):
    @property
    def provider_session_id(self) -> str: ...

    async def close(self) -> str | None: ...


class InvocationProvider(Protocol):
    async def describe(self) -> CapabilityDescriptor: ...

    async def start(self, request: InvocationRequest) -> ProviderHandle: ...


class PreparedInvocationProvider(InvocationProvider, Protocol):
    async def prepare_session(self, request: InvocationRequest) -> PreparedProviderSession: ...

    async def start_turn(self, prepared: PreparedProviderSession) -> ProviderHandle: ...


class PersistedProviderRecovery(Protocol):
    async def reconcile_persisted(
        self,
        request: InvocationRequest,
        provider_execution: ProviderExecutionRef,
    ) -> ReconcileResult: ...


class ModelCatalogProvider(Protocol):
    async def model_catalog(
        self, *, include_hidden: bool = False
    ) -> tuple[ModelDescriptor, ...]: ...


class InvocationGuard(Protocol):
    async def check(self, request: InvocationRequest) -> None: ...
