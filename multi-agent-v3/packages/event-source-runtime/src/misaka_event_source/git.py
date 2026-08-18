from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from misaka_kernel_contracts import JsonObject

from misaka_event_source.contracts import CloudEvent
from misaka_event_source.memory import MemoryEventSource


@dataclass(frozen=True, slots=True)
class GitPollerConfig:
    repository: Path
    branch: str
    source: str = "git.poller"
    event_type: str = "dev.misaka.git.commit.updated.v1"
    interval_seconds: float = 30.0
    emit_initial: bool = False

    def __post_init__(self) -> None:
        if not self.branch.strip() or not self.source.strip() or not self.event_type.strip():
            raise ValueError("branch, source and event_type must not be empty")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")


class GitBranchPoller:
    def __init__(
        self,
        config: GitPollerConfig,
        *,
        rev_parse: Callable[[Path, str], Awaitable[str]] | None = None,
    ) -> None:
        self.config = config
        self._rev_parse = rev_parse or _git_rev_parse
        self._source = MemoryEventSource()
        self._last_commit: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._errors: list[str] = []

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(self._errors)

    async def poll_once(self) -> CloudEvent | None:
        commit = await self._rev_parse(self.config.repository, self.config.branch)
        previous = self._last_commit
        self._last_commit = commit
        if previous is None and not self.config.emit_initial:
            return None
        if previous == commit:
            return None
        event = CloudEvent(
            event_id=f"git:{self.config.source}:{self.config.branch}:{commit}",
            source=self.config.source,
            event_type=self.config.event_type,
            subject=self.config.branch,
            data=cast(
                JsonObject,
                {
                    "repository": str(self.config.repository),
                    "branch": self.config.branch,
                    "commit": commit,
                    "previous_commit": previous,
                },
            ),
        )
        return await self._source.publish(event)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def events(self, *, start_sequence: int = 1):
        async for event in self._source.events(start_sequence=start_sequence):
            yield event

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._source.close()

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.poll_once()
            except Exception as exc:
                self._errors.append(str(exc))
            await asyncio.sleep(self.config.interval_seconds)


async def _git_rev_parse(repository: Path, branch: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repository),
        "rev-parse",
        f"refs/heads/{branch}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or "git rev-parse failed")
    return stdout.decode("utf-8").strip()
