from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from misaka_invocation_contracts import (
    CapabilityDescriptor,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    ModelDescriptor,
    ReconcileResult,
)


class ProviderHandle(Protocol):
    def events(self) -> AsyncIterator[InvocationEvent]: ...

    async def wait(self) -> InvocationResult: ...

    async def cancel(self, reason: str) -> None: ...

    async def reconcile(self) -> ReconcileResult: ...

    async def close(self) -> None: ...


class InvocationProvider(Protocol):
    async def describe(self) -> CapabilityDescriptor: ...

    async def start(self, request: InvocationRequest) -> ProviderHandle: ...


class ModelCatalogProvider(Protocol):
    async def model_catalog(
        self, *, include_hidden: bool = False
    ) -> tuple[ModelDescriptor, ...]: ...


class InvocationGuard(Protocol):
    async def check(self, request: InvocationRequest) -> None: ...
