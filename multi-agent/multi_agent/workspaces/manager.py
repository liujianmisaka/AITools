from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from multi_agent.domain.errors import WorkspaceNotAllowedError
from multi_agent.domain.models import AccessMode


class _AsyncReadWriteLock:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._writer and self._waiting_writers == 0
            )
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._writer and self._readers == 0
                )
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()


class WorkspaceManager:
    def __init__(self, workspaces: Mapping[str, Path | str]) -> None:
        self._workspaces: dict[str, Path] = {}
        self._locks: dict[str, _AsyncReadWriteLock] = {}
        for workspace_id, raw_path in workspaces.items():
            path = Path(raw_path).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise WorkspaceNotAllowedError(
                    f"workspace {workspace_id!r} is not a directory: {path}"
                )
            self._workspaces[workspace_id] = path
            self._locks[workspace_id] = _AsyncReadWriteLock()

    def resolve(self, workspace_id: str) -> Path:
        try:
            return self._workspaces[workspace_id]
        except KeyError as exc:
            raise WorkspaceNotAllowedError(
                f"workspace {workspace_id!r} is not allowlisted"
            ) from exc

    def describe(self) -> dict[str, str]:
        return {key: str(value) for key, value in sorted(self._workspaces.items())}

    @asynccontextmanager
    async def access(
        self,
        workspace_id: str,
        mode: AccessMode,
    ) -> AsyncIterator[Path]:
        path = self.resolve(workspace_id)
        lock = self._locks[workspace_id]
        context = lock.read() if mode == AccessMode.read_only else lock.write()
        async with context:
            yield path
