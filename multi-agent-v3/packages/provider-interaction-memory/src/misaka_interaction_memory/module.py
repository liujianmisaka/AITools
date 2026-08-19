from __future__ import annotations

from misaka_interaction_capability import INTERACTION_CHANNEL_SERVICE
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import ModuleId, ModuleManifest, ServiceProvision

from misaka_interaction_memory.store import MemoryInteractionChannelStore

MEMORY_INTERACTION_MODULE_ID = ModuleId("provider.interaction.memory")


class MemoryInteractionChannelModule:
    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=MEMORY_INTERACTION_MODULE_ID,
            version="1.0.0",
            provides=(ServiceProvision(INTERACTION_CHANNEL_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        context.provide(
            INTERACTION_CHANNEL_SERVICE,
            MemoryInteractionChannelStore(),
            version="1.0.0",
        )
        return None

    async def start(self, context: HostContext) -> None:
        del context
