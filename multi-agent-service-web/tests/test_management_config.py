from __future__ import annotations

from pathlib import Path

import pytest
from aitools_service_manager.config import ManagementConfig


def test_fake_profile_uses_aitools_owned_state_path(tmp_path: Path) -> None:
    config = ManagementConfig(root=tmp_path)

    assert (
        config.state_path
        == (tmp_path / ".data" / "multi-agent-v3" / "control-plane-fake.jsonl").resolve()
    )
    assert config.management_url == "http://127.0.0.1:8014"
    assert config.resolved_workspace_ids == ()


def test_codex_profile_requires_explicit_workspace_and_home(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="codex home"):
        ManagementConfig(root=tmp_path, profile="codex")

    with pytest.raises(ValueError, match="workspace root"):
        ManagementConfig(root=tmp_path, profile="codex", codex_home=tmp_path / "codex")


def test_ports_must_be_distinct(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ports must differ"):
        ManagementConfig(root=tmp_path, management_port=8016, control_plane_port=8016)
