from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from multi_agent.orchestration.engine import WorkflowEngine
from multi_agent.providers.fake import FakeProvider
from multi_agent.providers.registry import ProviderRegistry
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.workspaces.manager import WorkspaceManager


class EngineFixture:
    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.provider = FakeProvider()
        self.store = SQLiteStore(self.root / "state.sqlite3")
        self.engine = WorkflowEngine(
            store=self.store,
            providers=ProviderRegistry([self.provider]),
            workspaces=WorkspaceManager({"repo": self.workspace}),
            max_concurrency=8,
        )

    async def start(self) -> "EngineFixture":
        asyncio.get_running_loop().slow_callback_duration = 1.0
        await self.engine.start()
        return self

    async def close(self) -> None:
        await self.engine.close()
        self._temp.cleanup()


async def wait_for(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)
