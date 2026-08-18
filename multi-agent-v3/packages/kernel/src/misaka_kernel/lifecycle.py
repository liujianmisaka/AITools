from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from misaka_kernel.errors import LifecycleError

AsyncDisposer = Callable[[], Awaitable[None]]


class LifecycleScope:
    def __init__(self, name: str) -> None:
        if not name.strip():
            raise LifecycleError("scope.name_empty", "scope name must not be empty")
        self.name = name
        self._disposers: list[AsyncDisposer] = []
        self._children: list[LifecycleScope] = []
        self._closed = False
        self._closing = False
        self._close_lock = asyncio.Lock()
        self._closed_event = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed

    def add(self, disposer: AsyncDisposer) -> AsyncDisposer:
        if self._closed or self._closing:
            raise LifecycleError("scope.closed", f"scope {self.name} is closing or closed")
        self._disposers.append(disposer)
        return disposer

    def child(self, name: str) -> LifecycleScope:
        child = LifecycleScope(name)
        if self._closed or self._closing:
            raise LifecycleError("scope.closed", f"scope {self.name} is closing or closed")
        self._children.append(child)
        return child

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            if self._closing:
                wait_for_close = True
            else:
                self._closing = True
                wait_for_close = False

        if wait_for_close:
            await self._closed_event.wait()
            return

        failures: list[Exception] = []
        while self._children:
            child = self._children.pop()
            try:
                await child.close()
            except Exception as exc:
                failures.append(exc)
        while self._disposers:
            disposer = self._disposers.pop()
            try:
                await disposer()
            except Exception as exc:
                failures.append(exc)

        self._closed = True
        self._closed_event.set()
        if failures:
            raise ExceptionGroup(f"scope {self.name} disposal failed", failures)
