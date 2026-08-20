from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from misaka_kernel_contracts import JsonObject

from misaka_a2a_capability.contracts import (
    A2AAgentCard,
    TaskEvent,
    TaskRequest,
    TaskResult,
    TaskSnapshot,
    TaskStatus,
)


class TaskExecutionHandle(Protocol):
    @property
    def task_id(self) -> str: ...

    @property
    def invocation_id(self) -> str | None: ...

    @property
    def delegation_id(self) -> str | None: ...

    @property
    def activation_id(self) -> str | None: ...

    def events(self, *, start_sequence: int = 1) -> AsyncIterator[TaskEvent]: ...

    async def wait(self) -> TaskResult: ...

    async def cancel(self, reason: str) -> None: ...

    async def close(self) -> None: ...


class TaskHandler(Protocol):
    async def describe(self) -> A2AAgentCard: ...

    async def submit(self, request: TaskRequest) -> TaskExecutionHandle: ...


class RemoteTaskClient(Protocol):
    """Transport-neutral client port for a remote A2A Task endpoint."""

    async def describe(self) -> A2AAgentCard: ...

    async def submit(self, request: TaskRequest) -> TaskExecutionHandle: ...

    async def get(self, task_id: str) -> TaskSnapshot: ...

    async def close(self) -> None: ...


class TaskStore(Protocol):
    async def create(self, request: TaskRequest) -> tuple[TaskSnapshot, bool]: ...

    async def snapshot(self, task_id: str) -> TaskSnapshot: ...

    async def list_snapshots(self) -> tuple[TaskSnapshot, ...]: ...

    async def mark_working(
        self,
        task_id: str,
        delegation_id: str,
        *,
        invocation_id: str | None = None,
        activation_id: str | None = None,
    ) -> TaskSnapshot: ...

    async def append_event(
        self, task_id: str, status: TaskStatus, payload: JsonObject
    ) -> TaskEvent: ...

    async def finalize(self, result: TaskResult) -> TaskSnapshot: ...

    async def wait_terminal(self, task_id: str) -> TaskResult: ...

    def events(self, task_id: str, *, start_sequence: int = 1) -> AsyncIterator[TaskEvent]: ...
