from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

InputDisposition = Literal["discard", "inherit", "pipe"]
OutputDisposition = Literal["discard", "inherit", "pipe", "collect"]


class ProcessTerminationError(RuntimeError):
    code = "process.termination_unconfirmed"


@dataclass(frozen=True, slots=True)
class OutputCaptureSpec:
    disposition: OutputDisposition
    memory_limit_bytes: int
    spill_limit_bytes: int
    spill_directory: Path | None

    def __post_init__(self) -> None:
        if self.memory_limit_bytes <= 0:
            raise ValueError("memory output limit must be positive")
        if self.spill_limit_bytes < self.memory_limit_bytes:
            raise ValueError("spill output limit must be at least the memory limit")
        if self.disposition != "collect" and self.spill_directory is not None:
            raise ValueError("only collected output can use spill storage")
        if self.spill_directory is not None and not self.spill_directory.is_absolute():
            raise ValueError("spill directory must be absolute")


@dataclass(frozen=True, slots=True)
class ProcessSpawnSpec:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str | None]
    stdin: InputDisposition
    stdout: OutputCaptureSpec
    stderr: OutputCaptureSpec
    grace_seconds: float

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0].strip():
            raise ValueError("argv must contain an executable")
        if any(not argument or "\x00" in argument for argument in self.argv):
            raise ValueError("argv entries must be non-empty and contain no NUL")
        if not self.cwd.is_absolute():
            raise ValueError("process cwd must be absolute")
        if self.grace_seconds <= 0:
            raise ValueError("process grace period must be positive")
        normalized: dict[str, str | None] = {}
        for name, value in self.env.items():
            if not name or "\x00" in name or "=" in name:
                raise ValueError("environment names must be non-empty and contain no NUL or '='")
            if value is not None and "\x00" in value:
                raise ValueError("environment values must contain no NUL")
            normalized[name] = value
        object.__setattr__(self, "env", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class ProcessOutputRead:
    text: str
    next_offset: int
    lossy: bool
    spill_path: Path | None


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    exit_code: int | None
    signal: int | None
    timed_out: bool
    cancel_requested: bool
    cancel_confirmed: bool
    tree_quiescent: bool
    stdout_truncated: bool
    stderr_truncated: bool
    supervision: Literal["full", "partial"]
    supervision_detail: str | None = None
