from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aitools_service_manager import control_plane_host
from aitools_service_manager.config import (
    ProviderConfiguration,
    RuntimeConfiguration,
    RuntimeConfigurationStore,
    apply_claude_runtime_environment,
)
from fastapi import FastAPI


def test_fake_host_builds_real_v3_profile_from_persisted_configuration(
    tmp_path: Path,
) -> None:
    aitools_root = Path(__file__).resolve().parents[2]
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    configuration_path = tmp_path / "configuration.json"
    RuntimeConfigurationStore(configuration_path).save(
        RuntimeConfiguration(allowed_path_roots=(allowed,))
    )

    app = control_plane_host.create_control_plane_app(
        root=aitools_root,
        configuration_path=configuration_path,
    )

    assert app.title == "Misaka Multi-Agent V3 Control Plane"


def test_claude_runtime_environment_selects_opencodex_without_persisting_token() -> None:
    environment = {"OPENCODEX_TOKEN": "secret", "ANTHROPIC_BASE_URL": "old"}
    configuration = RuntimeConfiguration(
        claude_runtime_mode="opencodex",
        claude_opencodex_auth_token_env="OPENCODEX_TOKEN",
    )

    apply_claude_runtime_environment(configuration, environment)

    assert environment["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:10100"
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "secret"
    assert environment["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert "OPENCODEX_TOKEN" in environment


def test_claude_runtime_environment_clears_opencodex_route_for_native() -> None:
    environment = {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:10100",
        "ANTHROPIC_MODEL": "AIXW/gpt-5.6-sol",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "829800",
    }

    apply_claude_runtime_environment(RuntimeConfiguration(), environment)

    assert environment == {}


def test_claude_runtime_environment_uses_local_opencodex_default_token() -> None:
    configuration = RuntimeConfiguration(claude_runtime_mode="opencodex")
    environment: dict[str, str] = {}

    apply_claude_runtime_environment(configuration, environment)

    assert environment["ANTHROPIC_AUTH_TOKEN"] == "opencodex-proxy"
    assert environment["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:10100"


def test_claude_runtime_environment_requires_token_for_custom_gateway() -> None:
    configuration = RuntimeConfiguration(
        claude_runtime_mode="opencodex",
        claude_opencodex_base_url="https://gateway.example.test",
    )

    with pytest.raises(ValueError, match="requires a non-empty auth token"):
        apply_claude_runtime_environment(configuration, {})


def test_fake_host_passes_persisted_path_filter_to_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    configuration_path = tmp_path / "configuration.json"
    RuntimeConfigurationStore(configuration_path).save(
        RuntimeConfiguration(allowed_path_roots=(allowed,))
    )
    app = FastAPI()
    captured: dict[str, Any] = {}

    def build_app(**kwargs: Any) -> FastAPI:
        captured.update(kwargs)
        return app

    def fake_run_path(_path: str) -> dict[str, object]:
        return {"build_app": build_app}

    monkeypatch.setattr(control_plane_host, "run_path", fake_run_path)

    result = control_plane_host.create_control_plane_app(
        root=tmp_path,
        configuration_path=configuration_path,
    )

    assert result is app
    assert captured["allowed_path_roots"] == (allowed.resolve(),)
    assert captured["provider_configs"] == (ProviderConfiguration().to_profile_payload(),)
    assert captured["state_path"] == (tmp_path / ".data" / "multi-agent-v3" / "control-plane.jsonl")


def test_host_passes_all_persisted_providers_and_security_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    allowed = tmp_path / "allowed"
    codex_home.mkdir()
    allowed.mkdir()
    configuration_path = tmp_path / "configuration.json"
    RuntimeConfigurationStore(configuration_path).save(
        RuntimeConfiguration(
            providers=(
                ProviderConfiguration(provider_id="fake-local"),
                ProviderConfiguration(
                    provider_id="codex-local",
                    kind="codex",
                    codex_home=codex_home,
                    config_overrides=('model_provider="local"',),
                    network_deny_enforced=True,
                ),
            ),
            allowed_path_roots=(allowed,),
        )
    )
    app = FastAPI()
    captured: dict[str, Any] = {}

    def build_app(**kwargs: Any) -> FastAPI:
        captured.update(kwargs)
        return app

    def fake_run_path(_path: str) -> dict[str, object]:
        return {"build_app": build_app}

    monkeypatch.setattr(control_plane_host, "run_path", fake_run_path)

    result = control_plane_host.create_control_plane_app(
        root=tmp_path,
        configuration_path=configuration_path,
    )

    assert result is app
    assert captured["allowed_path_roots"] == (allowed.resolve(),)
    provider_configs = captured["provider_configs"]
    assert isinstance(provider_configs, tuple)
    assert provider_configs[0]["provider_id"] == "fake-local"
    assert provider_configs[1]["provider_id"] == "codex-local"
    assert provider_configs[1]["codex_home"] == codex_home.resolve()
    assert provider_configs[1]["config_overrides"] == ('model_provider="local"',)
    assert provider_configs[1]["network_deny_enforced"] is True
    assert captured["state_path"] == (tmp_path / ".data" / "multi-agent-v3" / "control-plane.jsonl")


def test_host_passes_persisted_claude_provider_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_config = tmp_path / "claude-config"
    claude_cli = tmp_path / "claude.exe"
    claude_config.mkdir()
    claude_cli.write_text("placeholder", encoding="utf-8")
    configuration_path = tmp_path / "configuration.json"
    RuntimeConfigurationStore(configuration_path).save(
        RuntimeConfiguration(
            providers=(
                ProviderConfiguration(
                    provider_id="claude-local",
                    kind="claude",
                    claude_config_dir=claude_config,
                    claude_cli_path=claude_cli,
                    model_ids=("claude-sonnet-4-5",),
                    network_deny_enforced=True,
                ),
            )
        )
    )
    captured: dict[str, Any] = {}

    def build_app(**kwargs: Any) -> FastAPI:
        captured.update(kwargs)
        return FastAPI()

    def fake_run_path(_path: str) -> dict[str, object]:
        return {"build_app": build_app}

    monkeypatch.setattr(control_plane_host, "run_path", fake_run_path)

    control_plane_host.create_control_plane_app(
        root=tmp_path,
        configuration_path=configuration_path,
    )

    provider = captured["provider_configs"][0]
    assert provider["kind"] == "claude"
    assert provider["claude_config_dir"] == claude_config.resolve()
    assert provider["claude_cli_path"] == claude_cli.resolve()
    assert provider["model_ids"] == ("claude-sonnet-4-5",)
