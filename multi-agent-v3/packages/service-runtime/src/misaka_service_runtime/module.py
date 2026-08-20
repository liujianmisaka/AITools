from __future__ import annotations

from typing import Protocol

from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import ModuleId, ModuleManifest, ServiceKey, ServiceProvision

from misaka_service_runtime.manager import ServiceManager, ServiceSnapshot

MANAGED_SERVICE_RUNTIME_SERVICE = ServiceKey("runtime.managed-service")
MANAGED_SERVICE_RUNTIME_MODULE_ID = ModuleId("runtime.managed-service")


class ManagedServiceRuntime(Protocol):
    @property
    def started(self) -> bool: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def list(self) -> tuple[ServiceSnapshot, ...]: ...

    async def get(self, service_id: str) -> ServiceSnapshot: ...

    async def start_service(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot: ...

    async def stop(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot: ...


class ManagedServiceRuntimeModule:
    """Binds a managed-service runtime to a Composition Kernel host."""

    def __init__(self, runtime: ManagedServiceRuntime | None = None) -> None:
        self.runtime = runtime or ServiceManager(())

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=MANAGED_SERVICE_RUNTIME_MODULE_ID,
            version="1.0.0",
            provides=(ServiceProvision(MANAGED_SERVICE_RUNTIME_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer:
        context.provide(
            MANAGED_SERVICE_RUNTIME_SERVICE,
            self.runtime,
            version="1.0.0",
        )

        async def dispose() -> None:
            await self.runtime.close()

        return dispose

    async def start(self, context: HostContext) -> None:
        del context
        await self.runtime.start()
