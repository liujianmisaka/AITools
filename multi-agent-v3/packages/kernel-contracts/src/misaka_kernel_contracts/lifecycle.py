from collections.abc import Awaitable, Callable
from typing import Protocol


class Disposer(Protocol):
    async def __call__(self) -> None: ...


AsyncDisposer = Callable[[], Awaitable[None]]

