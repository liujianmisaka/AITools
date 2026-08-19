from __future__ import annotations

import asyncio

from misaka_invocation_contracts import CapabilityDescriptor

from misaka_capability_catalog.contracts import (
    AsyncCleanup,
    CapabilityCatalog,
    ProviderRegistration,
    RegistrationHandle,
)
from misaka_capability_catalog.errors import (
    CapabilityCatalogAmbiguous,
    ProviderRegistrationConflict,
    ProviderRegistrationNotFound,
)


class MemoryCapabilityCatalog(CapabilityCatalog):
    def __init__(self) -> None:
        self._registrations: dict[str, _RegistrationHandle] = {}
        self._epochs: dict[str, int] = {}

    def register(
        self,
        provider_id: str,
        descriptor: CapabilityDescriptor,
        *,
        owner_id: str,
        scope_id: str,
        cleanup: AsyncCleanup | None = None,
    ) -> RegistrationHandle:
        _require_identifier(provider_id, "provider_id")
        _require_identifier(owner_id, "owner_id")
        _require_identifier(scope_id, "scope_id")
        if provider_id in self._registrations:
            raise ProviderRegistrationConflict(
                "capability.provider_duplicate",
                f"provider {provider_id} is already registered",
            )
        epoch = self._epochs.get(provider_id, 0) + 1
        registration = ProviderRegistration(
            registration_id=f"{provider_id}@{epoch}",
            provider_id=provider_id,
            descriptor=descriptor,
            epoch=epoch,
            owner_id=owner_id,
            scope_id=scope_id,
        )
        handle = _RegistrationHandle(self, registration, cleanup)
        self._registrations[provider_id] = handle
        self._epochs[provider_id] = epoch
        return handle

    def snapshot(self) -> tuple[ProviderRegistration, ...]:
        return tuple(
            handle.registration
            for handle in sorted(
                self._registrations.values(),
                key=lambda current: (current.registration.provider_id, current.registration.epoch),
            )
        )

    def find(
        self, capability_id: str, *, provider_id: str | None = None
    ) -> tuple[ProviderRegistration, ...]:
        _require_identifier(capability_id, "capability_id")
        if provider_id is not None:
            _require_identifier(provider_id, "provider_id")
        return tuple(
            registration
            for registration in self.snapshot()
            if registration.descriptor.capability_id == capability_id
            and (provider_id is None or registration.provider_id == provider_id)
        )

    def resolve(
        self, capability_id: str, *, provider_id: str | None = None
    ) -> ProviderRegistration:
        matches = self.find(capability_id, provider_id=provider_id)
        if not matches:
            raise ProviderRegistrationNotFound(
                "capability.not_found",
                f"no provider is registered for capability {capability_id}",
            )
        if len(matches) > 1:
            raise CapabilityCatalogAmbiguous(
                "capability.ambiguous",
                f"multiple providers are registered for capability {capability_id}",
            )
        return next(iter(matches))

    def remove(self, handle: _RegistrationHandle) -> None:
        current = self._registrations.get(handle.registration.provider_id)
        if current is handle:
            del self._registrations[handle.registration.provider_id]
            return
        if current is None:
            return
        raise ProviderRegistrationNotFound(
            "capability.registration_stale",
            f"registration {handle.registration.registration_id} is no longer active",
        )


class _RegistrationHandle:
    def __init__(
        self,
        catalog: MemoryCapabilityCatalog,
        registration: ProviderRegistration,
        cleanup: AsyncCleanup | None,
    ) -> None:
        self._catalog = catalog
        self._registration = registration
        self._cleanup = cleanup
        self._lock = asyncio.Lock()
        self._disposed = False

    @property
    def registration(self) -> ProviderRegistration:
        return self._registration

    async def dispose(self) -> None:
        async with self._lock:
            if self._disposed:
                return
            self._disposed = True
            self._catalog.remove(self)
            if self._cleanup is not None:
                await self._cleanup()


def _require_identifier(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
