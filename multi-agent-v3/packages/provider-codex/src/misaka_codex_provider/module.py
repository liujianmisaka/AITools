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
from misaka_session_capability import SESSION_STORE_SERVICE, SessionStore

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
                app_server_url=self.config.app_server_url,
                config_overrides=self.config.config_overrides,
                model_ids=self.config.model_ids,
                network_deny_enforced=self.config.network_deny_enforced,
                rpc_timeout_seconds=self.config.rpc_timeout_seconds,
                new_sessions_ephemeral=self.config.new_sessions_ephemeral,
                session_lease_ttl_seconds=self.config.session_lease_ttl_seconds,
                session_lease_renew_interval_seconds=(
                    self.config.session_lease_renew_interval_seconds
                ),
            )
        )

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=CODEX_AGENT_MODULE_ID,
            version="1.0.0",
            requires=(
                ServiceRequirement(INVOCATION_RUNTIME_SERVICE, version="1.0.0"),
                ServiceRequirement(SESSION_STORE_SERVICE, version="1.0.0"),
            ),
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
        session_store = cast(SessionStore, context.require(SESSION_STORE_SERVICE))
        self.provider.bind_session_store(session_store)
        disposer = await runtime.register_provider(
            self.provider_id,
            self.provider,
            owner_id=str(self.manifest.module_id),
            scope_id=context.scope_name,
        )
        try:
            service_disposer = context.provide(
                AGENT_PROVIDER_SERVICE,
                self.provider,
                version="1.0.0",
                name=self.provider_id,
            )
        except Exception:
            await disposer()
            raise

        async def dispose() -> None:
            await disposer()
            await service_disposer()

        return dispose

    async def start(self, context: HostContext) -> None:
        del context
