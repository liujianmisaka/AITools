from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process settings loaded exclusively from local configuration and environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MULTI_AGENT_V2_",
        extra="ignore",
        validate_default=True,
    )

    service_name: str = "multi-agent-v2-control-api"
    control_host: str = "127.0.0.1"
    control_port: int = Field(default=8011, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8021",
        "http://localhost:8021",
        "http://testserver",
    )

    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://multi_agent_app@127.0.0.1:5432/multi_agent_v2"),
        repr=False,
    )
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    artifact_root: Path = Path(".data/artifacts")
    dependency_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @field_validator("control_host")
    @classmethod
    def control_api_must_bind_to_loopback(cls, value: str) -> str:
        candidate = value.strip().lower()
        if candidate == "localhost":
            return candidate
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise ValueError("Control API host must be a loopback IP or localhost") from exc
        if not address.is_loopback:
            raise ValueError("Control API must only bind to a loopback address")
        if address.version != 4:
            raise ValueError("Control API currently supports IPv4 loopback bindings only")
        return candidate

    @field_validator("allowed_hosts")
    @classmethod
    def allowed_hosts_must_not_contain_wildcards(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("At least one trusted Host value is required")
        if any("*" in host for host in value):
            raise ValueError("Wildcard Host values are forbidden for the Control API")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def allowed_origins_must_be_explicit(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("At least one allowed Origin is required")
        for origin in value:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError("Origins must be explicit HTTP(S) origins without paths")
        return value

    @field_validator("artifact_root")
    @classmethod
    def normalize_artifact_root(cls, value: Path) -> Path:
        return value.expanduser().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
