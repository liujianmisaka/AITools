from __future__ import annotations

from pathlib import Path

import pytest
from misaka_codex_provider.models import CodexProviderConfig
from misaka_codex_provider.sdk import OpenAICodexSdk
from openai_codex import CodexConfig


class _Thread:
    id = "thread-1"


class _Codex:
    def __init__(self, config: CodexConfig) -> None:
        self.config = config
        self.start_kwargs: dict[str, object] | None = None
        self.resume_kwargs: dict[str, object] | None = None

    async def __aenter__(self) -> _Codex:
        return self

    async def close(self) -> None:
        return None

    async def thread_start(self, **kwargs: object) -> _Thread:
        self.start_kwargs = kwargs
        return _Thread()

    async def thread_resume(self, _thread_id: str, **kwargs: object) -> _Thread:
        self.resume_kwargs = kwargs
        return _Thread()

    async def models(self, *, include_hidden: bool = False) -> object:
        return {"include_hidden": include_hidden}


@pytest.mark.asyncio
async def test_codex_sdk_bridges_to_shared_app_server_and_routes_model_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[_Codex] = []

    def create(config: CodexConfig) -> _Codex:
        client = _Codex(config)
        captured.append(client)
        return client

    monkeypatch.setattr("misaka_codex_provider.sdk.AsyncCodex", create)
    sdk = OpenAICodexSdk(
        CodexProviderConfig(
            codex_home=tmp_path,
            app_server_url="ws://127.0.0.1:8048",
            config_overrides=('model_provider="pixel"',),
        )
    )
    client = sdk.create_client()

    async with client:
        await client.thread_start(
            approval_mode=sdk.approval_deny_all(),
            cwd=str(tmp_path),
            ephemeral=False,
            model="pixel/gpt-5.6-luna",
            sandbox=sdk.sandbox("read_only"),
        )

    native = captured[0]
    assert native.config.launch_args_override is not None
    assert native.config.launch_args_override[-2:] == (
        "--url",
        "ws://127.0.0.1:8048",
    )
    assert native.start_kwargs is not None
    assert native.start_kwargs["model_provider"] == "pixel"


def test_codex_provider_config_rejects_non_websocket_app_server_url() -> None:
    with pytest.raises(ValueError, match="WebSocket URL"):
        CodexProviderConfig(app_server_url="http://127.0.0.1:8048")
    with pytest.raises(ValueError, match="WebSocket URL"):
        CodexProviderConfig(app_server_url="ws://user:secret@127.0.0.1:8048")
