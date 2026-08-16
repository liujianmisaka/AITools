from __future__ import annotations

import json
from pathlib import Path

from multi_agent_v2.packages.agent_runtime.codex_locator import (
    CodexEnvironmentKind,
    CodexRuntimeLocator,
)


def test_locator_prefers_explicit_codex_home(tmp_path: Path) -> None:
    codex_home = tmp_path / "Open Codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model_provider = "sensenova"\nmodel_catalog_json = "opencodex-models.json"\n'
        '[model_providers.sensenova]\nname = "SenseNova"\n',
        encoding="utf-8",
    )
    (codex_home / "opencodex-models.json").write_text("{}", encoding="utf-8")

    descriptor = CodexRuntimeLocator(
        codex_home=codex_home,
        codex_bin="codex.exe",
        environ={},
        user_home=tmp_path,
    ).resolve()

    assert descriptor.codex_home == codex_home.resolve()
    assert descriptor.provider_id == "sensenova"
    assert descriptor.environment_kind == CodexEnvironmentKind.opencodex
    assert descriptor.config_source == "explicit"


def test_locator_reads_ccswitch_home_without_exposing_config(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    settings = tmp_path / ".cc-switch"
    settings.mkdir()
    (settings / "settings.json").write_text(
        json.dumps({"codexConfigDir": str(codex_home)}),
        encoding="utf-8",
    )

    descriptor = CodexRuntimeLocator(environ={}, user_home=tmp_path).resolve()

    assert descriptor.codex_home == codex_home.resolve()
    assert descriptor.config_source == "ccswitch_settings"
    assert descriptor.environment_kind == CodexEnvironmentKind.ccswitch


def test_locator_signature_changes_with_config(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model_provider = "openai"\n', encoding="utf-8")
    locator = CodexRuntimeLocator(codex_home=codex_home, environ={}, user_home=tmp_path)
    before = locator.resolve()
    config_path.write_text('model_provider = "ollama"\n', encoding="utf-8")

    after = locator.resolve()

    assert before.signature != after.signature
    assert after.provider_id == "ollama"
