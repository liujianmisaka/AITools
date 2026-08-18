from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

from misaka_kernel_contracts import JsonObject
from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    Sandbox,
)
from openai_codex.generated.v2_all import ReasoningEffort
from openai_codex.models import JsonObject as CodexJsonObject

from misaka_codex_provider.models import CodexProviderConfig
from misaka_codex_provider.native import (
    NativeClient,
    NativeNotification,
    NativeThread,
    NativeTurn,
)


class OpenAICodexSdk:
    def __init__(self, config: CodexProviderConfig) -> None:
        self._config = config

    def create_client(self) -> NativeClient:
        overrides = self._config.config_overrides
        if self._config.network_deny_enforced:
            overrides = (
                *overrides,
                'web_search="disabled"',
                "tools.web_search=false",
                "sandbox_workspace_write.network_access=false",
            )
        env = (
            {"CODEX_HOME": str(self._config.codex_home)}
            if self._config.codex_home is not None
            else None
        )
        config = CodexConfig(
            codex_bin=(str(self._config.codex_bin) if self._config.codex_bin is not None else None),
            config_overrides=overrides,
            env=env,
        )
        return _ClientAdapter(AsyncCodex(config))

    def approval_deny_all(self) -> object:
        return ApprovalMode.deny_all

    def sandbox(self, value: str) -> object:
        values = {
            "read_only": Sandbox.read_only,
            "workspace_write": Sandbox.workspace_write,
        }
        try:
            return values[value]
        except KeyError as exc:
            raise ValueError(f"unsupported Codex sandbox: {value}") from exc

    def effort(self, value: str) -> object:
        try:
            return ReasoningEffort(value)
        except ValueError as exc:
            raise ValueError(f"unsupported Codex reasoning effort: {value}") from exc


class _ClientAdapter:
    def __init__(self, client: AsyncCodex) -> None:
        self._client = client

    async def __aenter__(self) -> NativeClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        await self._client.close()

    async def thread_start(
        self,
        *,
        approval_mode: object,
        cwd: str,
        ephemeral: bool,
        model: str,
        sandbox: object,
    ) -> NativeThread:
        thread = await self._client.thread_start(
            approval_mode=cast(ApprovalMode, approval_mode),
            cwd=cwd,
            ephemeral=ephemeral,
            model=model,
            sandbox=cast(Sandbox, sandbox),
        )
        return _ThreadAdapter(thread)

    async def thread_resume(
        self,
        thread_id: str,
        *,
        approval_mode: object,
        cwd: str,
        model: str,
        sandbox: object,
    ) -> NativeThread:
        thread = await self._client.thread_resume(
            thread_id,
            approval_mode=cast(ApprovalMode, approval_mode),
            cwd=cwd,
            model=model,
            sandbox=cast(Sandbox, sandbox),
        )
        return _ThreadAdapter(thread)

    async def models(self, *, include_hidden: bool = False) -> object:
        return await self._client.models(include_hidden=include_hidden)


class _ThreadAdapter:
    def __init__(self, thread: AsyncThread) -> None:
        self._thread = thread
        self.id = thread.id

    async def turn(
        self,
        input: str,
        *,
        approval_mode: object,
        cwd: str,
        effort: object,
        model: str,
        output_schema: JsonObject | None,
        sandbox: object,
    ) -> NativeTurn:
        turn = await self._thread.turn(
            input,
            approval_mode=cast(ApprovalMode, approval_mode),
            cwd=cwd,
            effort=cast(ReasoningEffort, effort),
            model=model,
            output_schema=cast(CodexJsonObject | None, output_schema),
            sandbox=cast(Sandbox, sandbox),
        )
        return _TurnAdapter(turn)


class _TurnAdapter:
    def __init__(self, turn: AsyncTurnHandle) -> None:
        self._turn = turn
        self.id = turn.id

    async def stream(self) -> AsyncIterator[NativeNotification]:
        async for notification in self._turn.stream():
            yield cast(NativeNotification, notification)

    async def interrupt(self) -> object:
        return await self._turn.interrupt()
