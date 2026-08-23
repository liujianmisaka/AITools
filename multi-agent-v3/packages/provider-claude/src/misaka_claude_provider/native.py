from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from misaka_kernel_contracts import JsonObject

ToolPolicy = Callable[[str, Mapping[str, object]], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class NativeClaudeOptions:
    model: str
    effort: str
    cwd: str
    resume: str | None = None
    session_id: str | None = None
    cli_path: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    output_format: JsonObject | None = None
    tool_policy: ToolPolicy | None = None


class NativeClaudeClient(Protocol):
    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    def receive_messages(self) -> AsyncIterator[object]: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


class NativeClaudeSdk(Protocol):
    def create_client(self, options: NativeClaudeOptions) -> NativeClaudeClient: ...
