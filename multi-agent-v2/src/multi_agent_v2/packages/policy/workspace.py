from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type WorkspaceAccess = Literal["read_only", "workspace_write"]
type CleanupDisposition = Literal["removed", "preserved"]

_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
_DEFAULT_GIT_TIMEOUT_SECONDS = 30.0


class WorkspacePolicyError(RuntimeError):
    """Base class for rejected or unsafe workspace operations."""


class WorkspaceNotFoundError(WorkspacePolicyError):
    """Raised when an unknown workspace ID is requested."""


class UnsafeWorkspacePathError(WorkspacePolicyError):
    """Raised when a path crosses the configured local filesystem boundary."""


class WorkspaceGitError(WorkspacePolicyError):
    """Raised when Git cannot complete a bounded workspace operation."""


class WorkspaceReconciliationRequired(WorkspacePolicyError):
    """Raised when filesystem and Git worktree state cannot be reconciled safely."""


@dataclass(frozen=True, slots=True)
class WorkspaceDefinition:
    workspace_id: str
    root: Path
    worktree_root: Path
    base_ref: str = "HEAD"


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    workspace_id: str
    execution_id: str
    access: WorkspaceAccess
    path: Path
    repository_root: Path
    base_commit: str | None
    owns_worktree: bool
    reconciled: bool


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupResult:
    disposition: CleanupDisposition
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class PlannedWorktree:
    worktree_id: str
    workspace_id: str
    execution_id: str
    target_path: Path
    relative_path: str
    base_commit: str


class WorkspaceRegistry:
    """Immutable server-side mapping from public workspace IDs to local roots."""

    def __init__(self, definitions: Iterable[WorkspaceDefinition]) -> None:
        registered: dict[str, WorkspaceDefinition] = {}
        for definition in definitions:
            if not _WORKSPACE_ID.fullmatch(definition.workspace_id):
                raise WorkspacePolicyError("workspace ID must be 1-64 safe identifier characters")
            if definition.workspace_id in registered:
                raise WorkspacePolicyError(f"duplicate workspace ID: {definition.workspace_id}")
            if (
                not definition.base_ref.strip()
                or definition.base_ref.startswith("-")
                or "\x00" in definition.base_ref
            ):
                raise WorkspacePolicyError("workspace base ref must be a safe non-blank revision")

            root = _validated_existing_local_directory(definition.root, label="workspace root")
            worktree_root = _validated_existing_local_directory(
                definition.worktree_root,
                label="worktree root",
            )
            if _paths_overlap(root, worktree_root):
                raise UnsafeWorkspacePathError(
                    "worktree root and registered workspace root must not overlap"
                )
            registered[definition.workspace_id] = WorkspaceDefinition(
                workspace_id=definition.workspace_id,
                root=root,
                worktree_root=worktree_root,
                base_ref=definition.base_ref,
            )
        self._definitions = registered

    def get(self, workspace_id: str) -> WorkspaceDefinition:
        try:
            return self._definitions[workspace_id]
        except KeyError as exc:
            raise WorkspaceNotFoundError(f"unknown workspace ID: {workspace_id}") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


class WorkspaceSupervisor:
    """Creates and reconciles deterministic, isolated local Git worktrees."""

    def __init__(
        self,
        registry: WorkspaceRegistry,
        *,
        git_executable: str = "git",
        git_timeout_seconds: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        if not git_executable.strip() or "\x00" in git_executable:
            raise WorkspacePolicyError("Git executable must be non-blank")
        if git_timeout_seconds <= 0:
            raise WorkspacePolicyError("Git timeout must be positive")
        self._registry = registry
        self._git_executable = git_executable
        self._git_timeout_seconds = git_timeout_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def target_path(self, workspace_id: str, execution_id: str) -> Path:
        definition = self._registry.get(workspace_id)
        token = _execution_token(workspace_id, execution_id)
        candidate = definition.worktree_root / definition.workspace_id / token
        return _validated_contained_candidate(candidate, definition.worktree_root)

    async def prepare(
        self,
        workspace_id: str,
        execution_id: str,
        access: WorkspaceAccess,
    ) -> PreparedWorkspace:
        _validate_access(access)
        definition = self._registry.get(workspace_id)
        if access == "read_only":
            root = _validated_existing_local_directory(definition.root, label="workspace root")
            return PreparedWorkspace(
                workspace_id=workspace_id,
                execution_id=_validated_execution_id(execution_id),
                access=access,
                path=root,
                repository_root=root,
                base_commit=None,
                owns_worktree=False,
                reconciled=False,
            )
        return await self.materialize(await self.plan_write(workspace_id, execution_id))

    async def materialize(self, planned: PlannedWorktree) -> PreparedWorkspace:
        definition = self._registry.get(planned.workspace_id)
        execution_id = _validated_execution_id(planned.execution_id)
        expected_target = self.target_path(planned.workspace_id, execution_id)
        expected_token = _execution_token(planned.workspace_id, execution_id)
        expected_relative = expected_target.relative_to(definition.worktree_root).as_posix()
        if (
            planned.worktree_id != expected_token
            or _path_key(planned.target_path) != _path_key(expected_target)
            or planned.relative_path != expected_relative
            or not re.fullmatch(r"[0-9a-f]{40,64}", planned.base_commit)
        ):
            raise WorkspacePolicyError("persisted worktree plan does not match workspace policy")

        target = expected_target
        lock = await self._lock_for(target)
        async with lock:
            reconciled = await asyncio.to_thread(
                self._reconcile_write_sync,
                definition,
                execution_id,
                target,
            )
            if reconciled is not None:
                if reconciled.base_commit != planned.base_commit:
                    raise WorkspaceReconciliationRequired(
                        "existing worktree HEAD does not match the persisted base commit"
                    )
                return reconciled
            return await asyncio.to_thread(
                self._prepare_write_sync,
                definition,
                execution_id,
                target,
                planned.base_commit,
            )

    async def plan_write(self, workspace_id: str, execution_id: str) -> PlannedWorktree:
        definition = self._registry.get(workspace_id)
        target = self.target_path(workspace_id, execution_id)
        base_commit = await asyncio.to_thread(
            self._resolve_base_commit_sync,
            definition,
        )
        relative_path = target.relative_to(definition.worktree_root).as_posix()
        return PlannedWorktree(
            worktree_id=_execution_token(workspace_id, execution_id),
            workspace_id=workspace_id,
            execution_id=_validated_execution_id(execution_id),
            target_path=target,
            relative_path=relative_path,
            base_commit=base_commit,
        )

    async def reconcile(
        self,
        workspace_id: str,
        execution_id: str,
        access: WorkspaceAccess,
    ) -> PreparedWorkspace | None:
        _validate_access(access)
        definition = self._registry.get(workspace_id)
        if access == "read_only":
            root = _validated_existing_local_directory(definition.root, label="workspace root")
            return PreparedWorkspace(
                workspace_id=workspace_id,
                execution_id=_validated_execution_id(execution_id),
                access=access,
                path=root,
                repository_root=root,
                base_commit=None,
                owns_worktree=False,
                reconciled=True,
            )
        target = self.target_path(workspace_id, execution_id)
        lock = await self._lock_for(target)
        async with lock:
            return await asyncio.to_thread(
                self._reconcile_write_sync,
                definition,
                execution_id,
                target,
            )

    async def cleanup(
        self,
        prepared: PreparedWorkspace,
        *,
        preserve: bool = False,
    ) -> WorkspaceCleanupResult:
        definition = self._registry.get(prepared.workspace_id)
        if prepared.access == "read_only" or not prepared.owns_worktree:
            if prepared.path != definition.root:
                raise UnsafeWorkspacePathError(
                    "read-only workspace path no longer matches registry"
                )
            return WorkspaceCleanupResult(
                disposition="preserved",
                path=prepared.path,
                reason="registered read-only roots are never removed",
            )

        expected = self.target_path(prepared.workspace_id, prepared.execution_id)
        if _path_key(prepared.path) != _path_key(expected):
            raise UnsafeWorkspacePathError("prepared worktree path does not match execution ID")
        lock = await self._lock_for(expected)
        async with lock:
            return await asyncio.to_thread(
                self._cleanup_write_sync,
                definition,
                expected,
                preserve,
                prepared.base_commit,
            )

    async def _lock_for(self, path: Path) -> asyncio.Lock:
        key = _path_key(path)
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    def _prepare_write_sync(
        self,
        definition: WorkspaceDefinition,
        execution_id: str,
        target: Path,
        base_commit: str,
    ) -> PreparedWorkspace:
        repository_root = self._verified_repository_root(definition)
        target = _validated_contained_candidate(target, definition.worktree_root)
        if target.exists():
            raise WorkspaceReconciliationRequired(
                "deterministic worktree path exists without a matching Git registration"
            )

        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        _validate_existing_chain(parent)
        _assert_contained(parent, definition.worktree_root)

        self._git(
            repository_root,
            "worktree",
            "add",
            "--detach",
            os.fspath(target),
            base_commit,
        )
        try:
            safe_target = _validated_existing_local_directory(target, label="created worktree")
            _assert_contained(safe_target, definition.worktree_root)
            registered = self._registered_worktrees(repository_root)
            if _path_key(safe_target) not in registered:
                raise WorkspaceReconciliationRequired(
                    "Git did not register the newly created worktree"
                )
        except Exception:
            # Git owns any partially created directory. Preserve it for reconciliation.
            raise

        return PreparedWorkspace(
            workspace_id=definition.workspace_id,
            execution_id=_validated_execution_id(execution_id),
            access="workspace_write",
            path=safe_target,
            repository_root=repository_root,
            base_commit=base_commit.lower(),
            owns_worktree=True,
            reconciled=False,
        )

    def _resolve_base_commit_sync(self, definition: WorkspaceDefinition) -> str:
        repository_root = self._verified_repository_root(definition)
        base_commit = self._git(
            repository_root,
            "rev-parse",
            "--verify",
            f"{definition.base_ref}^{{commit}}",
        ).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_commit):
            raise WorkspaceGitError("Git returned an invalid base commit")
        return base_commit.lower()

    def _reconcile_write_sync(
        self,
        definition: WorkspaceDefinition,
        execution_id: str,
        target: Path,
    ) -> PreparedWorkspace | None:
        repository_root = self._verified_repository_root(definition)
        target = _validated_contained_candidate(target, definition.worktree_root)
        registered = self._registered_worktrees(repository_root)
        registration = registered.get(_path_key(target))
        exists = target.exists()
        if registration is None and not exists:
            return None
        if registration is None:
            _validate_existing_chain(target)
            raise WorkspaceReconciliationRequired(
                "worktree path exists but is not registered with Git"
            )
        if not exists:
            raise WorkspaceReconciliationRequired(
                "Git worktree registration exists but its directory is missing"
            )

        safe_target = _validated_existing_local_directory(target, label="reconciled worktree")
        _assert_contained(safe_target, definition.worktree_root)
        git_file = safe_target / ".git"
        if not git_file.is_file() or git_file.is_symlink() or _is_reparse_point(git_file):
            raise WorkspaceReconciliationRequired("worktree .git marker is not a safe regular file")
        base_commit = self._git(safe_target, "rev-parse", "--verify", "HEAD^{commit}").strip()
        return PreparedWorkspace(
            workspace_id=definition.workspace_id,
            execution_id=_validated_execution_id(execution_id),
            access="workspace_write",
            path=safe_target,
            repository_root=repository_root,
            base_commit=base_commit.lower(),
            owns_worktree=True,
            reconciled=True,
        )

    def _cleanup_write_sync(
        self,
        definition: WorkspaceDefinition,
        target: Path,
        preserve: bool,
        base_commit: str | None,
    ) -> WorkspaceCleanupResult:
        repository_root = self._verified_repository_root(definition)
        reconciled = self._reconcile_write_sync(definition, "cleanup", target)
        if reconciled is None:
            return WorkspaceCleanupResult(
                disposition="removed",
                path=target,
                reason="worktree was already absent",
            )
        if preserve:
            return WorkspaceCleanupResult(
                disposition="preserved",
                path=target,
                reason="caller requested preservation",
            )
        status = self._git(
            target,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
        if status:
            return WorkspaceCleanupResult(
                disposition="preserved",
                path=target,
                reason="worktree contains uncommitted changes",
            )
        current_commit = self._git(target, "rev-parse", "--verify", "HEAD^{commit}").strip()
        if base_commit is None or current_commit.lower() != base_commit.lower():
            return WorkspaceCleanupResult(
                disposition="preserved",
                path=target,
                reason="worktree HEAD changed from the prepared base commit",
            )

        _assert_contained(target, definition.worktree_root)
        self._git(repository_root, "worktree", "remove", os.fspath(target))
        if target.exists():
            raise WorkspaceReconciliationRequired(
                "Git reported successful removal but the worktree path remains"
            )
        return WorkspaceCleanupResult(
            disposition="removed",
            path=target,
            reason="clean worktree removed by Git",
        )

    def _verified_repository_root(self, definition: WorkspaceDefinition) -> Path:
        root = _validated_existing_local_directory(definition.root, label="workspace root")
        reported = self._git(root, "rev-parse", "--show-toplevel").strip()
        if not reported:
            raise WorkspaceGitError("Git did not report a repository root")
        repository_root = _validated_existing_local_directory(
            Path(reported),
            label="Git repository root",
        )
        if _path_key(repository_root) != _path_key(root):
            raise WorkspacePolicyError("registered workspace root must be the Git repository root")
        return repository_root

    def _registered_worktrees(self, repository_root: Path) -> dict[str, Path]:
        output = self._git(repository_root, "worktree", "list", "--porcelain", "-z")
        result: dict[str, Path] = {}
        for field in output.split("\x00"):
            if not field.startswith("worktree "):
                continue
            path = Path(field.removeprefix("worktree "))
            result[_path_key(path)] = path
        return result

    def _git(self, cwd: Path, *arguments: str) -> str:
        command = [self._git_executable, "-C", os.fspath(cwd), *arguments]
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._git_timeout_seconds,
                env=environment,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceGitError("bounded Git command could not be executed") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "Git command failed"
            raise WorkspaceGitError(detail[-1000:])
        return completed.stdout


def _validate_access(access: str) -> None:
    if access not in {"read_only", "workspace_write"}:
        raise WorkspacePolicyError(f"unsupported workspace access mode: {access}")


def _validated_execution_id(execution_id: str) -> str:
    if not execution_id.strip() or len(execution_id) > 512 or "\x00" in execution_id:
        raise WorkspacePolicyError("execution ID must be 1-512 non-blank characters")
    return execution_id


def _execution_token(workspace_id: str, execution_id: str) -> str:
    execution_id = _validated_execution_id(execution_id)
    digest = hashlib.sha256(f"{workspace_id}\x00{execution_id}".encode()).hexdigest()
    return digest[:32]


def _validated_existing_local_directory(path: Path, *, label: str) -> Path:
    _reject_unc(path, label=label)
    if not path.is_absolute():
        raise UnsafeWorkspacePathError(f"{label} must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise UnsafeWorkspacePathError(f"{label} does not exist") from exc
    _reject_unc(resolved, label=label)
    if not resolved.is_dir():
        raise UnsafeWorkspacePathError(f"{label} must be a directory")
    _validate_existing_chain(path)
    _validate_existing_chain(resolved)
    return resolved


def _validated_contained_candidate(candidate: Path, root: Path) -> Path:
    _reject_unc(candidate, label="worktree path")
    if not candidate.is_absolute():
        raise UnsafeWorkspacePathError("worktree path must be absolute")
    resolved = candidate.resolve(strict=False)
    _reject_unc(resolved, label="worktree path")
    _assert_contained(resolved, root)
    _validate_existing_chain(resolved.parent)
    return resolved


def _assert_contained(path: Path, root: Path) -> None:
    try:
        common = os.path.commonpath((os.fspath(path), os.fspath(root)))
    except ValueError as exc:
        raise UnsafeWorkspacePathError("path is on a different filesystem root") from exc
    if os.path.normcase(common) != os.path.normcase(os.fspath(root)):
        raise UnsafeWorkspacePathError("path escapes the configured worktree root")


def _paths_overlap(left: Path, right: Path) -> bool:
    left_key = _path_key(left)
    right_key = _path_key(right)
    try:
        common = os.path.normcase(os.path.commonpath((os.fspath(left), os.fspath(right))))
    except ValueError:
        return False
    return common in {left_key, right_key}


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path.resolve(strict=False))))


def _reject_unc(path: Path, *, label: str) -> None:
    raw = os.fspath(path)
    if raw.startswith(("\\\\", "//")):
        raise UnsafeWorkspacePathError(f"{label} must not use a UNC or device path")


def _validate_existing_chain(path: Path) -> None:
    current = path
    existing: list[Path] = []
    while True:
        if current.exists() or current.is_symlink():
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for component in reversed(existing):
        if component.is_symlink() or _is_reparse_point(component):
            raise UnsafeWorkspacePathError(
                f"workspace path contains a symbolic link or reparse point: {component}"
            )


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
