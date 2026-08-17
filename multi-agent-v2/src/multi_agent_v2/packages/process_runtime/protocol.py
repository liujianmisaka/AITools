from __future__ import annotations

from asyncio import Future
from typing import BinaryIO, Protocol

from multi_agent_v2.packages.process_runtime.models import (
    ProcessOutcome,
    ProcessOutputRead,
    ProcessSpawnSpec,
)


class ManagedProcess(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def create_time(self) -> float: ...

    @property
    def stdin(self) -> BinaryIO | None: ...

    @property
    def stdout(self) -> BinaryIO | None: ...

    @property
    def stderr(self) -> BinaryIO | None: ...

    @property
    def done(self) -> Future[ProcessOutcome]: ...

    def read_stdout(self, from_offset: int = 0) -> ProcessOutputRead | None: ...

    def read_stderr(self, from_offset: int = 0) -> ProcessOutputRead | None: ...

    async def terminate(self, *, timed_out: bool = False) -> ProcessOutcome: ...

    async def wait_for_exit(self, timeout_seconds: float | None = None) -> bool: ...

    async def aclose(self) -> None: ...


class ProcessRuntime(Protocol):
    async def spawn(self, spec: ProcessSpawnSpec) -> ManagedProcess: ...

    async def aclose(self) -> None: ...
