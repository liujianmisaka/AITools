from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from multi_agent_v2.packages.policy.workspace import (
    WorkspaceDefinition,
    WorkspaceRegistry,
)


class _WorkspaceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    workspace_id: str = Field(alias="id", min_length=1, max_length=64)
    root: Path
    worktree_root: Path = Field(alias="worktreeRoot")
    base_ref: str = Field(default="HEAD", alias="baseRef")


class _WorkspaceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaces: tuple[_WorkspaceEntry, ...]


def load_workspace_registry(path: Path) -> WorkspaceRegistry:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        config = _WorkspaceFile.model_validate(raw)
    except FileNotFoundError as exc:
        raise RuntimeError(f"workspace configuration does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise RuntimeError(f"workspace configuration is invalid: {path}") from exc

    base_directory = path.parent
    definitions: list[WorkspaceDefinition] = []
    for entry in config.workspaces:
        root = entry.root if entry.root.is_absolute() else base_directory / entry.root
        worktree_root = (
            entry.worktree_root
            if entry.worktree_root.is_absolute()
            else base_directory / entry.worktree_root
        )
        definitions.append(
            WorkspaceDefinition(
                workspace_id=entry.workspace_id,
                root=root.resolve(),
                worktree_root=worktree_root.resolve(),
                base_ref=entry.base_ref,
            )
        )
    return WorkspaceRegistry(definitions)
