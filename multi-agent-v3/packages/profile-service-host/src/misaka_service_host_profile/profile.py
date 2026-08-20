from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType

from misaka_kernel import (
    CompositionSnapshot,
    Host,
    HostStatus,
    ProfileDefinition,
    ProfileLoader,
)
from misaka_service_runtime import (
    MANAGED_SERVICE_RUNTIME_MODULE_ID,
    ManagedServiceRuntime,
    ManagedServiceRuntimeModule,
    ServiceDefinition,
    ServiceManager,
    ServiceSnapshot,
)


@dataclass(frozen=True, slots=True)
class ServiceHostConfig:
    profile_id: str = "service-host"
    profile_version: str = "1.0.0"
    transport_ids: tuple[str, ...] = ("service-management",)

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.profile_version.strip():
            raise ValueError("service host profile identity must not be empty")
        if any(not item.strip() for item in self.transport_ids):
            raise ValueError("service host transport ids must not be empty")
        if len(self.transport_ids) != len(set(self.transport_ids)):
            raise ValueError("service host transport ids must be unique")


class ServiceHost(AbstractAsyncContextManager["ServiceHost"]):
    """Composition profile for long-lived local services only."""

    def __init__(self, host: Host, runtime: ManagedServiceRuntime) -> None:
        self._host = host
        self.runtime = runtime
        self._lifecycle_lock = asyncio.Lock()

    @property
    def status(self) -> HostStatus:
        return self._host.status

    @property
    def composition_snapshot(self) -> CompositionSnapshot | None:
        return self._host.composition_snapshot

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._host.status is HostStatus.ACTIVE:
                return
            await self._host.start()

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._host.status is HostStatus.STOPPED:
                return
            await self._host.stop()

    async def services(self) -> tuple[ServiceSnapshot, ...]:
        self._require_started()
        return await self.runtime.list()

    async def start_service(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot:
        self._require_started()
        return await self.runtime.start_service(service_id, expected_epoch=expected_epoch)

    async def stop_service(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot:
        self._require_started()
        return await self.runtime.stop(service_id, expected_epoch=expected_epoch)

    async def __aenter__(self) -> ServiceHost:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.stop()

    def _require_started(self) -> None:
        if self._host.status is not HostStatus.ACTIVE:
            raise RuntimeError("service host must be active before using service operations")


def create_service_host(
    definitions: Iterable[ServiceDefinition] = (),
    *,
    runtime: ManagedServiceRuntime | None = None,
    config: ServiceHostConfig | None = None,
) -> ServiceHost:
    settings = config or ServiceHostConfig()
    definition_values = tuple(definitions)
    if runtime is not None and definition_values:
        raise ValueError("provide either definitions or runtime, not both")
    selected_runtime = runtime or ServiceManager(definition_values)
    loader = ProfileLoader(
        {MANAGED_SERVICE_RUNTIME_MODULE_ID: lambda: ManagedServiceRuntimeModule(selected_runtime)}
    )
    profile = ProfileDefinition(
        profile_id=settings.profile_id,
        profile_version=settings.profile_version,
        module_ids=(MANAGED_SERVICE_RUNTIME_MODULE_ID,),
        transport_ids=settings.transport_ids,
        fact_owners={"managed_service.lifecycle": "runtime.managed-service"},
        resource_owners={"service.process": "runtime.managed-service"},
    )
    return ServiceHost(loader.create_host(profile), selected_runtime)
