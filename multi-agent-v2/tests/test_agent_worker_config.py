from __future__ import annotations

import json
from pathlib import Path

import pytest

from multi_agent_v2.packages.policy import WorkspaceNotFoundError
from multi_agent_v2.workers.agent_windows import load_workspace_registry


def test_workspace_file_resolves_relative_paths_from_its_directory(tmp_path: Path) -> None:
    config_directory = tmp_path / "config"
    repository = config_directory / "repository"
    worktree_root = config_directory / "worktrees"
    config_directory.mkdir()
    repository.mkdir()
    worktree_root.mkdir()
    config_path = config_directory / "workspaces.json"
    config_path.write_text(
        json.dumps(
            {
                "workspaces": [
                    {
                        "id": "repo",
                        "root": "repository",
                        "worktreeRoot": "worktrees",
                        "baseRef": "main",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = load_workspace_registry(config_path)

    workspace = registry.get("repo")
    assert workspace.root == repository.resolve()
    assert workspace.worktree_root == worktree_root.resolve()
    assert workspace.base_ref == "main"
    with pytest.raises(WorkspaceNotFoundError):
        registry.get("missing")


def test_workspace_file_fails_closed_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    with pytest.raises(RuntimeError, match="does not exist"):
        load_workspace_registry(missing)
