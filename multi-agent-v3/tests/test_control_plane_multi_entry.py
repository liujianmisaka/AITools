from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from runpy import run_path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from misaka_codex_provider import CodexAgentProvider
from misaka_invocation_runtime import InvocationProvider
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


def _fake_configuration(provider_id: str) -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "kind": "fake",
        "codex_home": None,
        "config_overrides": (),
        "network_deny_enforced": False,
    }


def test_multi_provider_profile_registers_all_provider_catalogs(tmp_path: Path) -> None:
    app = _build_app(
        provider_configs=(
            _fake_configuration("pixel"),
            _fake_configuration("deepseek"),
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


def test_multi_provider_profile_rejects_duplicate_provider_ids() -> None:
    with pytest.raises(ValueError, match="provider ids must be unique"):
        _create_providers(
            (
                _fake_configuration("duplicate"),
                _fake_configuration("duplicate"),
            )
        )
