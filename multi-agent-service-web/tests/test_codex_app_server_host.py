from __future__ import annotations

from pathlib import Path

import pytest
from aitools_service_manager.codex_app_server_host import (
    app_server_command,
    app_server_environment,
)
from aitools_service_manager.config import (
    ProviderConfiguration,
    RuntimeConfiguration,
    RuntimeConfigurationStore,
)


def _save(
    path: Path,
    providers: tuple[ProviderConfiguration, ...],
) -> None:
    RuntimeConfigurationStore(path).save(RuntimeConfiguration(providers=providers))


def test_shared_app_server_loads_provider_definitions_and_routes_model_per_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration_path = tmp_path / "configuration.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    common = (
        'model_providers.pixel.base_url="http://127.0.0.1:10100/v1"',
        'model_providers.pixel.env_key="OPENAI_API_KEY"',
    )
    _save(
        configuration_path,
        (
            ProviderConfiguration(
                provider_id="pixel",
                kind="codex",
                codex_home=codex_home,
                config_overrides=('model_provider="pixel"', *common),
                model_ids=("pixel/gpt-5.6-luna",),
                network_deny_enforced=True,
            ),
            ProviderConfiguration(
                provider_id="deepseek",
                kind="codex",
                codex_home=codex_home,
                config_overrides=(
                    'model_provider="deepseek"',
                    *common,
                    'model_providers.deepseek.base_url="http://127.0.0.1:10100/v1"',
                    'model_providers.deepseek.env_key="OPENAI_API_KEY"',
                ),
                model_ids=("deepseek/deepseek-v3",),
                network_deny_enforced=True,
            ),
        ),
    )

    def find_codex(_name: str) -> str:
        return "C:/tools/codex.exe"

    monkeypatch.setattr(
        "aitools_service_manager.codex_app_server_host.shutil.which",
        find_codex,
    )

    command = app_server_command(configuration_path, listen_url="ws://127.0.0.1:8048")
    environment = app_server_environment(configuration_path)

    assert command[0] == "C:/tools/codex.exe"
    assert 'model_provider="pixel"' not in command
    assert 'model_provider="deepseek"' not in command
    assert common[0] in command
    assert "sandbox_workspace_write.network_access=false" in command
    assert command[-3:] == ("app-server", "--listen", "ws://127.0.0.1:8048")
    assert environment["CODEX_HOME"] == str(codex_home.resolve())
