from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class CodexProviderConfig:
    provider_id: str = "codex"
    codex_home: Path | None = None
    codex_bin: Path | None = None
    app_server_url: str | None = None
    config_overrides: tuple[str, ...] = ()
    model_ids: tuple[str, ...] = ()
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
        if self.app_server_url is not None:
            parsed = urlparse(self.app_server_url)
            if (
                parsed.scheme not in {"ws", "wss"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("app_server_url must be a WebSocket URL")
        normalized_models = tuple(model.strip() for model in self.model_ids)
        if any(not model for model in normalized_models):
            raise ValueError("model_ids must contain non-empty values")
        if len(normalized_models) != len(set(normalized_models)):
            raise ValueError("model_ids must be unique")
        object.__setattr__(self, "model_ids", normalized_models)
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
