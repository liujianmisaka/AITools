from __future__ import annotations

from typing import cast

from misaka_agent_capability import AGENT_PROVIDER_SERVICE
from misaka_invocation_runtime import (
    INVOCATION_RUNTIME_SERVICE,
    InvocationRuntime,
)
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    ModuleId,
    ModuleManifest,
    ServiceProvision,
    ServiceRequirement,
    ServiceShape,
)

from misaka_codex_provider.models import CodexProviderConfig
from misaka_codex_provider.provider import CodexAgentProvider

CODEX_AGENT_MODULE_ID = ModuleId("provider.agent.codex")


class CodexAgentModule:
    def __init__(
        self,
        config: CodexProviderConfig | None = None,
        *,
        provider_id: str | None = None,
    ) -> None:
        self.config = config or CodexProviderConfig()
        selected_provider_id = provider_id or self.config.provider_id
        if not selected_provider_id.strip():
            raise ValueError("provider_id must not be empty")
        self.provider_id = selected_provider_id
        self.provider = CodexAgentProvider(
            CodexProviderConfig(
                provider_id=selected_provider_id,
                codex_home=self.config.codex_home,
                codex_bin=self.config.codex_bin,
                workspace_roots=self.config.workspace_roots,
                config_overrides=self.config.config_overrides,
                network_deny_enforced=self.config.network_deny_enforced,
                rpc_timeout_seconds=self.config.rpc_timeout_seconds,
                new_sessions_ephemeral=self.config.new_sessions_ephemeral,
            )
        )

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=CODEX_AGENT_MODULE_ID,
            version="1.0.0",
            requires=(ServiceRequirement(INVOCATION_RUNTIME_SERVICE, version="1.0.0"),),
            provides=(
                ServiceProvision(
                    AGENT_PROVIDER_SERVICE,
                    "1.0.0",
                    shape=ServiceShape.NAMED,
                    name=self.provider_id,
                ),
            ),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        runtime = cast(InvocationRuntime, context.require(INVOCATION_RUNTIME_SERVICE))
        await runtime.register_provider(self.provider_id, self.provider)
        context.provide(
            AGENT_PROVIDER_SERVICE,
            self.provider,
            version="1.0.0",
            name=self.provider_id,
        )
        return None

    async def start(self, context: HostContext) -> None:
        del context
