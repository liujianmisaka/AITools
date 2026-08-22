from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CodexProviderConfig:
    provider_id: str = "codex"
    codex_home: Path | None = None
    codex_bin: Path | None = None
    config_overrides: tuple[str, ...] = ()
    network_deny_enforced: bool = False
    rpc_timeout_seconds: float = 15.0
    new_sessions_ephemeral: bool = False
    session_lease_ttl_seconds: float = 30.0
    session_lease_renew_interval_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if self.rpc_timeout_seconds <= 0:
            raise ValueError("rpc_timeout_seconds must be positive")
        if self.session_lease_ttl_seconds <= 0:
            raise ValueError("session_lease_ttl_seconds must be positive")
        if (
            self.session_lease_renew_interval_seconds is not None
            and self.session_lease_renew_interval_seconds <= 0
        ):
            raise ValueError("session_lease_renew_interval_seconds must be positive")
        if (
            self.session_lease_renew_interval_seconds is not None
            and self.session_lease_renew_interval_seconds >= self.session_lease_ttl_seconds
        ):
            raise ValueError(
                "session_lease_renew_interval_seconds must be shorter than the lease ttl"
            )


@dataclass(frozen=True, slots=True)
class CodexModel:
    id: str
    display_name: str
    description: str
    supported_efforts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodexModelCatalog:
    models: tuple[CodexModel, ...]
