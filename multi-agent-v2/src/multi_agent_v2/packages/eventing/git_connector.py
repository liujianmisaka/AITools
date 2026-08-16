from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

import psutil

from multi_agent_v2.packages.control_plane.models import GitRefTarget
from multi_agent_v2.packages.domain.events import CloudEventEnvelope, EventIngestResult
from multi_agent_v2.packages.persistence import (
    ControlPlaneConflict,
    ControlPlaneRepository,
    RevisionConflict,
)
from multi_agent_v2.packages.policy import WorkspaceRegistry
from multi_agent_v2.packages.workflow_dsl.canonical import canonical_json, sha256_text

_COMMIT_ID = re.compile(r"^[0-9a-fA-F]{40,64}$")


class GitConnectorError(RuntimeError):
    """A bounded Git ref check could not produce a trustworthy result."""


@dataclass(frozen=True, slots=True)
class GitPollResult:
    initialized: bool
    changed: bool
    previous_commit: str | None
    current_commit: str
    event: CloudEventEnvelope | None
    ingest: EventIngestResult | None


class GitRefPoller:
    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        workspaces: WorkspaceRegistry,
        git_executable: str = "git",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not git_executable.strip() or "\x00" in git_executable:
            raise ValueError("Git executable must be a safe non-blank value")
        if timeout_seconds <= 0:
            raise ValueError("Git connector timeout must be positive")
        self._repository = repository
        self._workspaces = workspaces
        self._git_executable = git_executable
        self._timeout_seconds = timeout_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def poll(self, target: GitRefTarget) -> GitPollResult:
        lock = await self._connector_lock(target.connector_id)
        async with lock:
            return await self._poll_locked(target)

    async def _poll_locked(self, target: GitRefTarget) -> GitPollResult:
        workspace = self._workspaces.get(target.workspace_id)
        configuration_hash = sha256_text(
            canonical_json(
                {
                    "workspaceId": target.workspace_id,
                    "remote": target.remote,
                    "branch": target.branch,
                }
            )
        )
        for _ in range(3):
            checkpoint = await self._repository.get_connector_checkpoint(target.connector_id)
            if checkpoint is not None and (
                checkpoint.connector_kind != "git_ref"
                or checkpoint.configuration_hash != configuration_hash
            ):
                raise ControlPlaneConflict(
                    "Git connector ID is already bound to another configuration"
                )
            current_commit = await asyncio.to_thread(
                _read_remote_commit,
                self._git_executable,
                workspace.root,
                target.remote,
                target.branch,
                self._timeout_seconds,
            )
            previous_commit = checkpoint.checkpoint_value if checkpoint is not None else None
            if previous_commit == current_commit:
                return GitPollResult(
                    initialized=False,
                    changed=False,
                    previous_commit=previous_commit,
                    current_commit=current_commit,
                    event=None,
                    ingest=None,
                )
            if previous_commit is None:
                try:
                    await self._repository.advance_connector_checkpoint(
                        connector_id=target.connector_id,
                        connector_kind="git_ref",
                        configuration_hash=configuration_hash,
                        checkpoint_value=current_commit,
                        expected_previous=None,
                    )
                except RevisionConflict:
                    continue
                return GitPollResult(
                    initialized=True,
                    changed=False,
                    previous_commit=None,
                    current_commit=current_commit,
                    event=None,
                    ingest=None,
                )

            event = _git_commit_event(
                target,
                previous_commit=previous_commit,
                current_commit=current_commit,
            )
            ingest = await self._repository.ingest_event(event)
            try:
                await self._repository.advance_connector_checkpoint(
                    connector_id=target.connector_id,
                    connector_kind="git_ref",
                    configuration_hash=configuration_hash,
                    checkpoint_value=current_commit,
                    expected_previous=previous_commit,
                )
            except RevisionConflict:
                continue
            return GitPollResult(
                initialized=False,
                changed=True,
                previous_commit=previous_commit,
                current_commit=current_commit,
                event=event,
                ingest=ingest,
            )
        raise GitConnectorError("Git connector checkpoint changed repeatedly during polling")

    async def _connector_lock(self, connector_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(connector_id, asyncio.Lock())


def _git_commit_event(
    target: GitRefTarget,
    *,
    previous_commit: str,
    current_commit: str,
) -> CloudEventEnvelope:
    return CloudEventEnvelope(
        id=f"{target.connector_id}:{current_commit}",
        source=f"urn:misaka:git:{target.connector_id}",
        type="dev.misaka.git.commit.updated.v1",
        subject=f"refs/heads/{target.branch}",
        time=datetime.now(UTC),
        data={
            "connectorId": target.connector_id,
            "workspaceId": target.workspace_id,
            "remote": target.remote,
            "branch": target.branch,
            "previousCommit": previous_commit,
            "currentCommit": current_commit,
        },
    )


def _read_remote_commit(
    git_executable: str,
    repository_root: os.PathLike[str],
    remote: str,
    branch: str,
    timeout_seconds: float,
) -> str:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [
            git_executable,
            "ls-remote",
            "--exit-code",
            "--heads",
            remote,
            f"refs/heads/{branch}",
        ],
        cwd=repository_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(process.pid)
        process.kill()
        process.communicate()
        raise GitConnectorError("Git ref check timed out") from exc
    if process.returncode != 0:
        detail = stderr.strip()[:512]
        raise GitConnectorError(
            f"Git ref check failed with exit code {process.returncode}: {detail}"
        )
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise GitConnectorError("Git ref check returned an unexpected number of refs")
    commit, separator, returned_ref = lines[0].partition("\t")
    expected_ref = f"refs/heads/{branch}"
    if separator != "\t" or returned_ref != expected_ref or not _COMMIT_ID.fullmatch(commit):
        raise GitConnectorError("Git ref check returned an invalid ref record")
    return commit.lower()


def _kill_process_tree(process_id: int) -> None:
    try:
        process = psutil.Process(process_id)
        children = process.children(recursive=True)
    except (psutil.Error, OSError):
        return
    for child in reversed(children):
        try:
            child.kill()
        except (psutil.Error, OSError):
            continue
