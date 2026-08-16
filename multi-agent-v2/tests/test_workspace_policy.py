from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from multi_agent_v2.packages.policy.workspace import (
    PreparedWorkspace,
    UnsafeWorkspacePathError,
    WorkspaceDefinition,
    WorkspaceNotFoundError,
    WorkspacePolicyError,
    WorkspaceReconciliationRequired,
    WorkspaceRegistry,
    WorkspaceSupervisor,
)


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(cwd), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    worktree_root = tmp_path / "worktrees"
    repository.mkdir()
    worktree_root.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Workspace Test")
    _git(repository, "config", "user.email", "workspace@example.invalid")
    (repository / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "test: initialize repository")
    return repository.resolve(), worktree_root.resolve()


def _supervisor(tmp_path: Path) -> tuple[WorkspaceSupervisor, Path, Path]:
    repository, worktree_root = _repository(tmp_path)
    registry = WorkspaceRegistry(
        [
            WorkspaceDefinition(
                workspace_id="repo",
                root=repository,
                worktree_root=worktree_root,
            )
        ]
    )
    return WorkspaceSupervisor(registry), repository, worktree_root


def test_registry_rejects_unknown_unc_and_overlapping_roots(tmp_path: Path) -> None:
    repository, worktree_root = _repository(tmp_path)
    registry = WorkspaceRegistry([WorkspaceDefinition("repo", repository, worktree_root)])
    assert registry.ids() == ("repo",)
    with pytest.raises(WorkspaceNotFoundError):
        registry.get("missing")
    with pytest.raises(UnsafeWorkspacePathError):
        WorkspaceRegistry(
            [
                WorkspaceDefinition(
                    "unc",
                    Path(r"\\server\share\repository"),
                    worktree_root,
                )
            ]
        )
    with pytest.raises(WorkspacePolicyError):
        WorkspaceRegistry(
            [WorkspaceDefinition("unsafe-ref", repository, worktree_root, base_ref="--all")]
        )
    with pytest.raises(UnsafeWorkspacePathError):
        WorkspaceRegistry(
            [
                WorkspaceDefinition(
                    "overlap",
                    repository,
                    repository / ".worktrees",
                )
            ]
        )


@pytest.mark.asyncio
async def test_read_only_workspace_never_creates_or_removes_worktree(tmp_path: Path) -> None:
    supervisor, repository, worktree_root = _supervisor(tmp_path)

    prepared = await supervisor.prepare("repo", "read-execution", "read_only")
    reconciled = await supervisor.reconcile("repo", "read-execution", "read_only")
    cleanup = await supervisor.cleanup(prepared)

    assert prepared.path == repository
    assert prepared.owns_worktree is False
    assert reconciled is not None and reconciled.path == repository
    assert reconciled.reconciled is True
    assert cleanup.disposition == "preserved"
    assert not any(worktree_root.iterdir())


@pytest.mark.asyncio
async def test_write_workspace_is_deterministic_reconciled_and_cleaned(tmp_path: Path) -> None:
    supervisor, repository, worktree_root = _supervisor(tmp_path)
    execution_id = "workflow/node:1 with unsafe / path text"

    first = await supervisor.prepare("repo", execution_id, "workspace_write")
    second = await supervisor.prepare("repo", execution_id, "workspace_write")

    assert first.path == second.path == supervisor.target_path("repo", execution_id)
    assert first.path.parent.parent == worktree_root
    assert first.path != repository
    assert first.owns_worktree is True
    assert first.reconciled is False
    assert second.reconciled is True
    assert first.base_commit == _git(repository, "rev-parse", "HEAD")
    assert _git(first.path, "rev-parse", "--is-inside-work-tree") == "true"

    (first.path / "README.md").write_text("changed\n", encoding="utf-8")
    dirty_cleanup = await supervisor.cleanup(first)
    assert dirty_cleanup.disposition == "preserved"
    assert first.path.exists()

    _git(first.path, "restore", "README.md")
    removed = await supervisor.cleanup(first)
    assert removed.disposition == "removed"
    assert not first.path.exists()
    assert await supervisor.reconcile("repo", execution_id, "workspace_write") is None


@pytest.mark.asyncio
async def test_preserve_keeps_clean_worktree_available_for_reconciliation(tmp_path: Path) -> None:
    supervisor, _, _ = _supervisor(tmp_path)
    prepared = await supervisor.prepare("repo", "preserved", "workspace_write")

    preserved = await supervisor.cleanup(prepared, preserve=True)
    reconciled = await supervisor.reconcile("repo", "preserved", "workspace_write")

    assert preserved.disposition == "preserved"
    assert reconciled is not None
    assert reconciled.path == prepared.path
    assert reconciled.reconciled is True

    removed = await supervisor.cleanup(prepared)
    assert removed.disposition == "removed"


@pytest.mark.asyncio
async def test_existing_unregistered_target_is_preserved_for_reconciliation(tmp_path: Path) -> None:
    supervisor, _, _ = _supervisor(tmp_path)
    target = supervisor.target_path("repo", "conflict")
    target.mkdir(parents=True)
    marker = target / "do-not-delete.txt"
    marker.write_text("preserve me", encoding="utf-8")

    with pytest.raises(WorkspaceReconciliationRequired):
        await supervisor.prepare("repo", "conflict", "workspace_write")

    assert marker.read_text(encoding="utf-8") == "preserve me"


@pytest.mark.asyncio
async def test_cleanup_rejects_a_spoofed_worktree_path(tmp_path: Path) -> None:
    supervisor, repository, _ = _supervisor(tmp_path)
    prepared = PreparedWorkspace(
        workspace_id="repo",
        execution_id="write-execution",
        access="workspace_write",
        path=repository,
        repository_root=repository,
        base_commit=_git(repository, "rev-parse", "HEAD"),
        owns_worktree=True,
        reconciled=False,
    )

    with pytest.raises(UnsafeWorkspacePathError):
        await supervisor.cleanup(prepared)

    assert repository.exists()


@pytest.mark.asyncio
async def test_missing_registered_worktree_requires_reconciliation(tmp_path: Path) -> None:
    supervisor, _, _ = _supervisor(tmp_path)
    prepared = await supervisor.prepare("repo", "missing", "workspace_write")
    moved = prepared.path.with_name(f"{prepared.path.name}-moved")
    prepared.path.rename(moved)
    try:
        with pytest.raises(WorkspaceReconciliationRequired):
            await supervisor.reconcile("repo", "missing", "workspace_write")
    finally:
        moved.rename(prepared.path)

    removed = await supervisor.cleanup(prepared)
    assert removed.disposition == "removed"


@pytest.mark.asyncio
async def test_concurrent_prepare_converges_on_one_worktree(tmp_path: Path) -> None:
    supervisor, repository, _ = _supervisor(tmp_path)

    left, right = await asyncio.gather(
        supervisor.prepare("repo", "same-execution", "workspace_write"),
        supervisor.prepare("repo", "same-execution", "workspace_write"),
    )

    assert left.path == right.path
    assert sorted((left.reconciled, right.reconciled)) == [False, True]
    worktrees = _git(repository, "worktree", "list", "--porcelain")
    assert worktrees.count("worktree ") == 2

    await supervisor.cleanup(left)


@pytest.mark.asyncio
async def test_materialize_uses_the_persisted_base_commit(tmp_path: Path) -> None:
    supervisor, repository, _ = _supervisor(tmp_path)
    planned = await supervisor.plan_write("repo", "fixed-base")

    (repository / "README.md").write_text("branch moved\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "test: move branch after planning")

    prepared = await supervisor.materialize(planned)

    assert prepared.base_commit == planned.base_commit
    assert _git(prepared.path, "rev-parse", "HEAD") == planned.base_commit
    assert _git(repository, "rev-parse", "HEAD") != planned.base_commit
    await supervisor.cleanup(prepared)


def test_registry_rejects_symbolic_link_root_when_supported(tmp_path: Path) -> None:
    repository, worktree_root = _repository(tmp_path)
    linked_root = tmp_path / "linked-repository"
    try:
        linked_root.symlink_to(repository, target_is_directory=True)
    except OSError:
        pytest.skip("directory symbolic links are unavailable on this host")

    with pytest.raises(UnsafeWorkspacePathError):
        WorkspaceRegistry([WorkspaceDefinition("linked", linked_root, worktree_root)])
