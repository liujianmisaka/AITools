from __future__ import annotations

import asyncio
import hashlib
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from multi_agent.domain.errors import (
    EventSourceNotFoundError,
    TriggerEventProcessingError,
)
from multi_agent.domain.models import (
    GitCommitSourceConfig,
    TriggerBindingDefinition,
    TriggerEventInput,
    utc_now,
)
from multi_agent.workspaces.manager import WorkspaceManager


@dataclass(frozen=True, slots=True)
class SourcePollResult:
    events: tuple[TriggerEventInput, ...]
    cursor: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class EventSourceDriver(ABC):
    source_type: str
    delivery_mode: str

    def validate_binding(self, binding: TriggerBindingDefinition) -> None:
        del binding

    def describe(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "delivery_mode": self.delivery_mode,
            "supports_polling": self.delivery_mode in {"poll", "hybrid"},
            "supports_push": self.delivery_mode in {"push", "hybrid"},
        }

    async def poll(
        self,
        binding: Mapping[str, Any],
        cursor: Mapping[str, Any] | None,
    ) -> SourcePollResult:
        del binding, cursor
        raise RuntimeError(f"event source {self.source_type!r} is not pollable")


class ManualEventSource(EventSourceDriver):
    source_type = "manual"
    delivery_mode = "push"


class GitCommitEventSource(EventSourceDriver):
    source_type = "git_commit"
    delivery_mode = "poll"

    def __init__(
        self,
        *,
        workspaces: WorkspaceManager,
        git_bin: str = "git",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._workspaces = workspaces
        self._git_bin = git_bin
        self._timeout_seconds = timeout_seconds

    def validate_binding(self, binding: TriggerBindingDefinition) -> None:
        if binding.event_type != "git.commit.updated" or binding.event_version != 1:
            raise TriggerEventProcessingError(
                "git_commit bindings must use git.commit.updated@1"
            )
        try:
            config = GitCommitSourceConfig.model_validate(binding.source_config)
        except ValidationError as exc:
            raise TriggerEventProcessingError(
                f"invalid git_commit source_config: {exc}"
            ) from exc
        self._workspaces.resolve(config.workspace_id)
        expected_key = self.source_key(config)
        if binding.source_key != expected_key:
            raise TriggerEventProcessingError(
                "git_commit source_key must equal "
                f"{expected_key!r} for the configured workspace/remote/branch"
            )

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "event_types": ["git.commit.updated@1"],
            "source_config_schema": GitCommitSourceConfig.model_json_schema(),
            "first_poll": "establish_baseline",
        }

    @classmethod
    def source_key(cls, config: GitCommitSourceConfig) -> str:
        return f"{config.workspace_id}:{config.remote}:{config.branch}"

    async def poll(
        self,
        binding: Mapping[str, Any],
        cursor: Mapping[str, Any] | None,
    ) -> SourcePollResult:
        config = GitCommitSourceConfig.model_validate(binding["source_config"])
        workspace = self._workspaces.resolve(config.workspace_id)
        if config.fetch:
            refspec = (
                f"+refs/heads/{config.branch}:"
                f"refs/remotes/{config.remote}/{config.branch}"
            )
            await self._git(
                workspace,
                "fetch",
                "--quiet",
                "--no-tags",
                "--prune",
                config.remote,
                refspec,
            )
        remote_ref = f"refs/remotes/{config.remote}/{config.branch}"
        after_sha = (
            await self._git(workspace, "rev-parse", "--verify", remote_ref)
        ).strip()
        if not self._is_sha(after_sha):
            raise TriggerEventProcessingError(
                f"git returned an invalid commit SHA for {remote_ref!r}"
            )
        previous_sha = str((cursor or {}).get("head_sha", ""))
        next_cursor = {
            "head_sha": after_sha,
            "observed_at": utc_now().isoformat(),
        }
        if not previous_sha or previous_sha == after_sha:
            return SourcePollResult(events=(), cursor=next_cursor)

        forward = await self._is_ancestor(workspace, previous_sha, after_sha)
        subject = (
            await self._git(workspace, "show", "-s", "--format=%s", after_sha)
        ).strip()
        author_name = (
            await self._git(workspace, "show", "-s", "--format=%an", after_sha)
        ).strip()
        authored_at = (
            await self._git(workspace, "show", "-s", "--format=%aI", after_sha)
        ).strip()
        commit_count: int | None = None
        if forward:
            raw_count = (
                await self._git(
                    workspace,
                    "rev-list",
                    "--count",
                    f"{previous_sha}..{after_sha}",
                )
            ).strip()
            commit_count = int(raw_count)
        source_key = self.source_key(config)
        dedup_material = f"{source_key}\0{previous_sha}\0{after_sha}".encode()
        event = TriggerEventInput(
            source_type=self.source_type,
            event_type="git.commit.updated",
            event_version=1,
            source_key=source_key,
            dedup_key="git:" + hashlib.sha256(dedup_material).hexdigest(),
            payload={
                "workspace_id": config.workspace_id,
                "remote": config.remote,
                "branch": config.branch,
                "before_sha": previous_sha,
                "after_sha": after_sha,
                "update_kind": "forward" if forward else "rewritten",
                "commit_count": commit_count,
                "subject": subject,
                "author_name": author_name,
                "authored_at": authored_at,
                "observed_at": utc_now().astimezone(timezone.utc).isoformat(),
            },
        )
        return SourcePollResult(events=(event,), cursor=next_cursor)

    async def _is_ancestor(
        self,
        workspace: Path,
        before_sha: str,
        after_sha: str,
    ) -> bool:
        process = await self._run_git(
            workspace,
            "merge-base",
            "--is-ancestor",
            before_sha,
            after_sha,
            check=False,
        )
        if process.returncode == 0:
            return True
        if process.returncode == 1:
            return False
        raise TriggerEventProcessingError(
            self._git_error(process, "git merge-base failed")
        )

    async def _git(self, workspace: Path, *arguments: str) -> str:
        process = await self._run_git(workspace, *arguments, check=True)
        return process.stdout.decode("utf-8", errors="replace")

    async def _run_git(
        self,
        workspace: Path,
        *arguments: str,
        check: bool,
    ) -> _GitCommandResult:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    self._git_bin,
                    "-C",
                    str(workspace),
                    *arguments,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
        except TimeoutError as exc:
            raise TriggerEventProcessingError(
                f"git command timed out after {self._timeout_seconds} seconds"
            ) from exc
        except OSError as exc:
            raise TriggerEventProcessingError(
                f"cannot start git executable {self._git_bin!r}: {exc}"
            ) from exc
        if check and process.returncode != 0:
            raise TriggerEventProcessingError(
                self._git_error(
                    _GitCommandResult(process.returncode, stdout, stderr),
                    "git command failed",
                )
            )
        return _GitCommandResult(process.returncode, stdout, stderr)

    @staticmethod
    def _git_error(process: _GitCommandResult, fallback: str) -> str:
        stderr = process.stderr.decode("utf-8", errors="replace").strip()
        return stderr or fallback

    @staticmethod
    def _is_sha(value: str) -> bool:
        return len(value) in {40, 64} and all(
            char in "0123456789abcdef" for char in value
        )


class FakeEventSource(EventSourceDriver):
    """Deterministic poll source used by tests; it performs no external I/O."""

    source_type = "fake"
    delivery_mode = "hybrid"

    def __init__(self) -> None:
        self._events: dict[str, list[TriggerEventInput]] = defaultdict(list)

    def emit(self, event: TriggerEventInput) -> None:
        if event.source_type != self.source_type:
            raise ValueError("fake source can only emit source_type='fake'")
        self._events[event.source_key or ""].append(event)

    async def poll(
        self,
        binding: Mapping[str, Any],
        cursor: Mapping[str, Any] | None,
    ) -> SourcePollResult:
        source_key = str(binding.get("source_key") or "")
        offset = int((cursor or {}).get("offset", 0))
        events = self._events[source_key][offset:]
        return SourcePollResult(
            events=tuple(events),
            cursor={"offset": offset + len(events)},
        )


class EventSourceRegistry:
    def __init__(self, sources: Iterable[EventSourceDriver] = ()) -> None:
        self._sources: dict[str, EventSourceDriver] = {}
        for source in sources:
            self.register(source)

    def register(self, source: EventSourceDriver) -> None:
        if not source.source_type:
            raise ValueError("event source type cannot be empty")
        if source.delivery_mode not in {"push", "poll", "hybrid"}:
            raise ValueError(
                f"invalid delivery mode for {source.source_type!r}: "
                f"{source.delivery_mode!r}"
            )
        if source.source_type in self._sources:
            raise ValueError(
                f"event source already registered: {source.source_type}"
            )
        self._sources[source.source_type] = source

    def get(self, source_type: str) -> EventSourceDriver:
        try:
            return self._sources[source_type]
        except KeyError as exc:
            raise EventSourceNotFoundError(
                f"event source not found: {source_type}"
            ) from exc

    def describe(self) -> list[dict[str, object]]:
        return [
            source.describe()
            for source in sorted(
                self._sources.values(), key=lambda item: item.source_type
            )
        ]
