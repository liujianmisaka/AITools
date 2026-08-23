from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True, slots=True)
class ClaudeProviderConfig:
    provider_id: str = "claude"
    claude_config_dir: Path | None = None
    cli_path: Path | None = None
    model_ids: tuple[str, ...] = ()
    network_deny_enforced: bool = False
    rpc_timeout_seconds: float = 60.0
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
        normalized_models = tuple(model.strip() for model in self.model_ids)
        if any(not model for model in normalized_models):
            raise ValueError("model_ids must contain non-empty values")
        if len(normalized_models) != len(set(normalized_models)):
            raise ValueError("model_ids must be unique")
        object.__setattr__(self, "model_ids", normalized_models)


@dataclass(frozen=True, slots=True)
class ClaudeModelCatalog:
    models: tuple[str, ...]
