from __future__ import annotations

from pathlib import Path

import pytest
from aitools_service_manager.catalog import control_plane_command
from aitools_service_manager.config import (
    ManagementConfig,
    RuntimeConfiguration,
    RuntimeConfigurationStore,
)


def test_management_config_uses_aitools_owned_runtime_configuration(tmp_path: Path) -> None:
    config = ManagementConfig(root=tmp_path)

    assert (
        config.configuration_path
        == (tmp_path / ".data" / "aitools-service-manager" / "configuration.json").resolve()
    )
    assert config.management_url == "http://127.0.0.1:8014"
    assert config.initial_runtime_configuration == RuntimeConfiguration()
    assert (
        config.control_plane_state_path("fake")
        == (tmp_path / ".data" / "multi-agent-v3" / "control-plane-fake.jsonl").resolve()
    )


def test_runtime_configuration_requires_valid_codex_home_and_allowed_roots(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    allowed = tmp_path / "allowed"
    codex_home.mkdir()
    allowed.mkdir()

    configuration = RuntimeConfiguration(
        profile="codex",
        codex_home=codex_home,
        allowed_path_roots=(allowed,),
    )

    assert configuration.codex_home == codex_home.resolve()
    assert configuration.allowed_path_roots == (allowed.resolve(),)
    with pytest.raises(ValueError, match="codex home"):
        RuntimeConfiguration(profile="codex")
    with pytest.raises(ValueError, match="absolute"):
        RuntimeConfiguration(allowed_path_roots=(Path("relative"),))
    with pytest.raises(ValueError, match="unavailable"):
        RuntimeConfiguration(allowed_path_roots=(tmp_path / "missing",))


def test_runtime_configuration_store_persists_and_reloads_exact_settings(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    allowed = tmp_path / "allowed"
    codex_home.mkdir()
    allowed.mkdir()
    store = RuntimeConfigurationStore(tmp_path / "configuration.json")
    expected = RuntimeConfiguration(
        profile="codex",
        codex_home=codex_home,
        provider_id="codex-local",
        network_deny_enforced=True,
        allowed_path_roots=(allowed,),
    )

    store.save(expected)

    assert store.load() == expected


def test_control_plane_command_reads_persisted_configuration_at_start(tmp_path: Path) -> None:
    command = control_plane_command(ManagementConfig(root=tmp_path))

    assert command[1:3] == ("-m", "aitools_service_manager.control_plane_host")
    assert "--configuration-path" in command
    assert "--workspace-root" not in command
    assert "--workspace-id" not in command
    assert "--allowed-path-root" not in command


def test_ports_must_be_distinct(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ports must differ"):
        ManagementConfig(root=tmp_path, management_port=8016, control_plane_port=8016)
