from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Fixed application context shared by all MCP tool calls."""

    control_plane_url: str = "http://127.0.0.1:8016"
    provider_id: str | None = None
    model: str | None = None
    effort: str | None = None
    actor_id: str = "mcp-client"
    actor_kind: str = "application"
    scope_id: str = "mcp"
    sandbox: str = "read_only"
    network_policy: str = "deny"
    capability_id: str = "agent.invocation"
    operation: str = "invoke"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        url = self.control_plane_url.rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("control_plane_url must be an absolute HTTP(S) URL")
        object.__setattr__(self, "control_plane_url", url)
        for field_name in (
            "actor_id",
            "actor_kind",
            "scope_id",
            "capability_id",
            "operation",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.sandbox not in {"read_only", "workspace_write"}:
            raise ValueError("sandbox must be read_only or workspace_write")
        if self.network_policy not in {"allow", "deny"}:
            raise ValueError("network_policy must be allow or deny")
        if self.actor_kind not in {
            "human",
            "application",
            "agent",
            "service",
            "system",
        }:
            raise ValueError("actor_kind is not a supported principal kind")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def actor(self) -> dict[str, str]:
        return {
            "principal_id": self.actor_id,
            "kind": self.actor_kind,
        }

    @property
    def scope(self) -> dict[str, str]:
        return {"scope_id": self.scope_id}
