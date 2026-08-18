from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Protocol

from misaka_kernel_contracts import JsonObject


class NativeNotification(Protocol):
    method: str
    payload: object


class NativeTurn(Protocol):
    id: str

    def stream(self) -> AsyncIterator[NativeNotification]: ...

    async def interrupt(self) -> object: ...


class NativeThread(Protocol):
    id: str

    async def turn(
        self,
        input: str,
        *,
        approval_mode: object,
        cwd: str,
        effort: object,
        model: str,
        output_schema: JsonObject | None,
        sandbox: object,
    ) -> NativeTurn: ...


class NativeClient(Protocol):
    async def __aenter__(self) -> NativeClient: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...

    async def thread_start(
        self,
        *,
        approval_mode: object,
        cwd: str,
        ephemeral: bool,
        model: str,
        sandbox: object,
    ) -> NativeThread: ...

    async def thread_resume(
        self,
        thread_id: str,
        *,
        approval_mode: object,
        cwd: str,
        model: str,
        sandbox: object,
    ) -> NativeThread: ...

    async def models(self, *, include_hidden: bool = False) -> object: ...


class NativeSdk(Protocol):
    def create_client(self) -> NativeClient: ...

    def approval_deny_all(self) -> object: ...

    def sandbox(self, value: str) -> object: ...

    def effort(self, value: str) -> object: ...


NativeSdkFactory = Callable[[], NativeSdk]
