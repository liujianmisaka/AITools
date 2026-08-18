from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CodexProviderConfig:
    provider_id: str = "codex"
    codex_home: Path | None = None
    codex_bin: Path | None = None
    workspace_roots: tuple[Path, ...] = ()
    config_overrides: tuple[str, ...] = ()
    network_deny_enforced: bool = False

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be empty")
        for root in self.workspace_roots:
            if not root.is_absolute():
                raise ValueError("workspace roots must be absolute paths")


@dataclass(frozen=True, slots=True)
class CodexModel:
    id: str
    display_name: str
    description: str
    supported_efforts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodexModelCatalog:
    models: tuple[CodexModel, ...]
