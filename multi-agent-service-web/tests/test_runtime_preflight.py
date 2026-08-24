from __future__ import annotations

from pathlib import Path

import pytest
from aitools_service_manager.config import ProviderConfiguration, RuntimeConfiguration
from aitools_service_manager.runtime_preflight import (
    ProviderRuntimeAccessError,
    validate_provider_runtime_access,
)


def test_codex_runtime_preflight_probes_and_cleans_configured_home(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    configuration = RuntimeConfiguration(
        providers=(
            ProviderConfiguration(
                provider_id="codex-local",
                kind="codex",
                codex_home=codex_home,
            ),
        )
    )

    validate_provider_runtime_access(configuration)

    assert list(codex_home.iterdir()) == []


def test_codex_runtime_preflight_reports_provider_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    configuration = RuntimeConfiguration(
        providers=(
            ProviderConfiguration(
                provider_id="codex-local",
                kind="codex",
                codex_home=codex_home,
            ),
        )
    )

    def reject_probe(_path: Path) -> None:
        raise PermissionError("access denied")

    monkeypatch.setattr(
        "aitools_service_manager.runtime_preflight._probe_writable_directory",
        reject_probe,
    )

    with pytest.raises(ProviderRuntimeAccessError) as caught:
        validate_provider_runtime_access(configuration)

    assert caught.value.provider_id == "codex-local"
    assert caught.value.path == codex_home.resolve()
    assert "access denied" in str(caught.value)
