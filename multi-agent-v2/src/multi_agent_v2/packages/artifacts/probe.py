from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path


class ArtifactRootProbe:
    name = "artifact_root"

    def __init__(self, root: Path) -> None:
        self._root = root

    async def check(self) -> None:
        await asyncio.to_thread(self._check_sync)

    def _check_sync(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise NotADirectoryError(self._root)

        descriptor, probe_path = tempfile.mkstemp(prefix=".health-", dir=self._root)
        try:
            os.write(descriptor, b"ok")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            Path(probe_path).unlink(missing_ok=True)
