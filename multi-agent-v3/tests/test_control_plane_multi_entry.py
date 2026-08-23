from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from runpy import run_path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from misaka_claude_provider import ClaudeAgentProvider
from misaka_codex_provider import CodexAgentProvider
from misaka_invocation_runtime import InvocationProvider, InvocationRuntime
from misaka_session_capability import MemorySessionStore

_ENTRY = run_path(str(Path(__file__).parents[1] / "examples" / "control_plane_multi.py"))
_build_app = cast(Callable[..., FastAPI], _ENTRY["build_app"])
_create_providers = cast(
    Callable[
        [tuple[Mapping[str, object], ...]],
        tuple[tuple[str, InvocationProvider], ...],
    ],
    _ENTRY["_create_providers"],
)
_register_providers = cast(
    Callable[
        [InvocationRuntime, tuple[tuple[str, InvocationProvider], ...]],
        Awaitable[None],
    ],
    _ENTRY["_register_providers"],
)


class _FailingProvider:
    async def describe(self) -> object:
        raise RuntimeError("provider description failed")


def _fake_configuration(provider_id: str) -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "kind": "fake",
        "codex_home": None,
        "config_overrides": (),
        "network_deny_enforced": False,
    }


def test_multi_provider_profile_registers_all_provider_catalogs(tmp_path: Path) -> None:
    claude_config_dir = tmp_path / "claude-config"
    claude_config_dir.mkdir()
    app = _build_app(
        provider_configs=(
            _fake_configuration("pixel"),
            _fake_configuration("deepseek"),
            {
                "provider_id": "claude",
                "kind": "claude",
                "codex_home": None,
                "config_overrides": (),
                "claude_config_dir": claude_config_dir,
                "claude_cli_path": None,
                "model_ids": ("claude-sonnet-4-5",),
                "network_deny_enforced": True,
            },
        ),
        state_path=tmp_path / "control-plane.jsonl",
        allowed_path_roots=(tmp_path,),
    )

    with TestClient(app) as client:
        response = client.get("/models")

    assert response.status_code == 200
    assert [catalog["provider_id"] for catalog in response.json()] == [
        "pixel",
        "deepseek",
        "claude",
    ]


def test_multi_provider_profile_builds_isolated_codex_provider(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    providers = _create_providers(
        (
            {
                "provider_id": "codex-local",
                "kind": "codex",
                "codex_home": codex_home,
                "config_overrides": ('model_provider="local"',),
                "network_deny_enforced": True,
            },
        )
    )

    provider_id, provider = providers[0]
    assert provider_id == "codex-local"
    assert isinstance(provider, CodexAgentProvider)
    assert provider.config.config_overrides == ('model_provider="local"',)
    assert provider.config.network_deny_enforced
    assert isinstance(provider.session_store, MemorySessionStore)


def test_multi_provider_profile_builds_claude_provider(tmp_path: Path) -> None:
    claude_config_dir = tmp_path / "claude-config"
    claude_config_dir.mkdir()
    providers = _create_providers(
        (
            {
                "provider_id": "claude-local",
                "kind": "claude",
                "codex_home": None,
                "config_overrides": (),
                "claude_config_dir": claude_config_dir,
                "claude_cli_path": None,
                "model_ids": ("claude-sonnet-4-5",),
                "network_deny_enforced": True,
            },
        )
    )

    provider_id, provider = providers[0]
    assert provider_id == "claude-local"
    assert isinstance(provider, ClaudeAgentProvider)
    assert provider.config.claude_config_dir == claude_config_dir.resolve()
    assert provider.config.model_ids == ("claude-sonnet-4-5",)


def test_multi_provider_profile_requires_claude_model_catalog() -> None:
    with pytest.raises(ValueError, match="at least one model id"):
        _create_providers(
            (
                {
                    "provider_id": "claude-empty",
                    "kind": "claude",
                    "codex_home": None,
                    "config_overrides": (),
                    "claude_config_dir": None,
                    "claude_cli_path": None,
                    "model_ids": (),
                    "network_deny_enforced": True,
                },
            )
        )


def test_multi_provider_profile_rejects_duplicate_provider_ids() -> None:
    with pytest.raises(ValueError, match="provider ids must be unique"):
        _create_providers(
            (
                _fake_configuration("duplicate"),
                _fake_configuration("duplicate"),
            )
        )


@pytest.mark.asyncio
async def test_multi_provider_registration_rolls_back_partial_failure() -> None:
    runtime = InvocationRuntime()
    registered_provider = _create_providers((_fake_configuration("ok"),))[0][1]
    providers = (
        ("registered", registered_provider),
        ("broken", cast(InvocationProvider, _FailingProvider())),
    )

    with pytest.raises(RuntimeError, match="provider description failed"):
        await _register_providers(runtime, providers)

    assert runtime.descriptors() == ()
