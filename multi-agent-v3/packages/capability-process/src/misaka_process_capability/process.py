from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import ModuleId, ModuleManifest, ServiceKey, ServiceProvision

PROCESS_SUPERVISOR_SERVICE = ServiceKey("capability.process.supervisor")
FAKE_PROCESS_MODULE_ID = ModuleId("capability.process.fake")


class ProcessState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    argv: tuple[str, ...]
    cwd: str | None = None
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.argv or any(not item.strip() for item in self.argv):
            raise ValueError("process argv must contain non-empty arguments")


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    create_time: float

    def __post_init__(self) -> None:
        if self.pid < 1 or self.create_time < 0:
            raise ValueError("process identity must contain a valid pid and create time")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    state: ProcessState
    exit_code: int | None = None
    error: str | None = None


class ProcessHandle(Protocol):
    @property
    def identity(self) -> ProcessIdentity: ...

    async def wait(self) -> ProcessResult: ...

    async def terminate(self, reason: str) -> None: ...


class ProcessSupervisor(Protocol):
    async def start(self, spec: ProcessSpec) -> ProcessHandle: ...


class FakeProcessSupervisor:
    def __init__(self, *, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.starts = 0
        self.last_handle: FakeProcessHandle | None = None

    async def start(self, spec: ProcessSpec) -> FakeProcessHandle:
        self.starts += 1
        handle = FakeProcessHandle(
            ProcessIdentity(self.starts, float(self.starts)),
            self.exit_code,
        )
        self.last_handle = handle
        del spec
        return handle


class FakeProcessHandle:
    def __init__(self, identity: ProcessIdentity, exit_code: int) -> None:
        self._identity = identity
        self._exit_code = exit_code
        self._done = asyncio.Event()
        self._terminated = False

    @property
    def identity(self) -> ProcessIdentity:
        return self._identity

    async def wait(self) -> ProcessResult:
        await self._done.wait()
        if self._terminated:
            return ProcessResult(ProcessState.CANCELLED, error="terminated")
        state = ProcessState.SUCCEEDED if self._exit_code == 0 else ProcessState.FAILED
        return ProcessResult(state, exit_code=self._exit_code)

    async def terminate(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("termination reason must not be empty")
        self._terminated = True
        self._done.set()

    def complete(self) -> None:
        self._done.set()


class FakeProcessModule:
    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=FAKE_PROCESS_MODULE_ID,
            version="1.0.0",
            provides=(ServiceProvision(PROCESS_SUPERVISOR_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        context.provide(
            PROCESS_SUPERVISOR_SERVICE,
            FakeProcessSupervisor(),
            version="1.0.0",
        )
        return None

    async def start(self, context: HostContext) -> None:
        del context
