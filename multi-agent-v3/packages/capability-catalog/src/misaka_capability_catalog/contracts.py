from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from misaka_invocation_contracts import CapabilityDescriptor

AsyncCleanup = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    registration_id: str
    provider_id: str
    descriptor: CapabilityDescriptor
    epoch: int
    owner_id: str
    scope_id: str

    def __post_init__(self) -> None:
        for name, value in {
            "registration_id": self.registration_id,
            "provider_id": self.provider_id,
            "owner_id": self.owner_id,
            "scope_id": self.scope_id,
        }.items():
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.epoch < 1:
            raise ValueError("epoch must be positive")


class RegistrationHandle(Protocol):
    @property
    def registration(self) -> ProviderRegistration: ...

    async def dispose(self) -> None: ...


class CapabilityCatalog(Protocol):
    def register(
        self,
        provider_id: str,
        descriptor: CapabilityDescriptor,
        *,
        owner_id: str,
        scope_id: str,
        cleanup: AsyncCleanup | None = None,
    ) -> RegistrationHandle: ...

    def snapshot(self) -> tuple[ProviderRegistration, ...]: ...

    def find(
        self, capability_id: str, *, provider_id: str | None = None
    ) -> tuple[ProviderRegistration, ...]: ...

    def resolve(
        self, capability_id: str, *, provider_id: str | None = None
    ) -> ProviderRegistration: ...
