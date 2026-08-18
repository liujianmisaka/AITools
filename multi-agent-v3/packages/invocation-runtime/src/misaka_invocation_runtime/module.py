from __future__ import annotations

from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import ModuleId, ModuleManifest, ServiceProvision

from misaka_invocation_runtime.runtime import InvocationRuntime
from misaka_invocation_runtime.services import INVOCATION_RUNTIME_SERVICE

INVOCATION_RUNTIME_MODULE_ID = ModuleId("runtime.invocation")


class InvocationRuntimeModule:
    def __init__(self, runtime: InvocationRuntime | None = None) -> None:
        self.runtime = runtime or InvocationRuntime()

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=INVOCATION_RUNTIME_MODULE_ID,
            version="1.0.0",
            provides=(ServiceProvision(INVOCATION_RUNTIME_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer:
        context.provide(
            INVOCATION_RUNTIME_SERVICE,
            self.runtime,
            version="1.0.0",
        )

        async def dispose() -> None:
            await self.runtime.stop()

        return dispose

    async def start(self, context: HostContext) -> None:
        del context
