from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from multi_agent.domain.errors import WorkspaceNotAllowedError
from multi_agent.domain.models import AccessMode
from multi_agent.workspaces.manager import WorkspaceManager


class WorkspaceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_allows_parallel_readers_and_excludes_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = WorkspaceManager({"repo": Path(temporary_directory)})
            readers_entered = 0
            both_reading = asyncio.Event()
            release_readers = asyncio.Event()
            writer_entered = asyncio.Event()

            async def reader() -> None:
                nonlocal readers_entered
                async with manager.access("repo", AccessMode.read_only):
                    readers_entered += 1
                    if readers_entered == 2:
                        both_reading.set()
                    await release_readers.wait()

            async def writer() -> None:
                async with manager.access("repo", AccessMode.workspace_write):
                    writer_entered.set()

            first = asyncio.create_task(reader())
            second = asyncio.create_task(reader())
            await asyncio.wait_for(both_reading.wait(), timeout=1)
            write_task = asyncio.create_task(writer())
            await asyncio.sleep(0.02)
            self.assertFalse(writer_entered.is_set())
            release_readers.set()
            await asyncio.gather(first, second, write_task)
            self.assertTrue(writer_entered.is_set())

    async def test_rejects_unknown_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = WorkspaceManager({"repo": Path(temporary_directory)})
            with self.assertRaises(WorkspaceNotAllowedError):
                manager.resolve("outside")
