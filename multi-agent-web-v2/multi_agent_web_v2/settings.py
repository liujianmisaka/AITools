from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MULTI_AGENT_WEB_V2_",
        extra="ignore",
        validate_default=True,
    )

    public_host: str = "127.0.0.1"
    public_port: int = Field(default=8021, ge=1, le=65535)
    internal_host: str = "127.0.0.1"
    internal_port: int = Field(default=8022, ge=1, le=65535)
    control_api_url: str = "http://127.0.0.1:8011"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8021",
        "http://localhost:8021",
        "http://testserver",
    )
    internal_stream_token: SecretStr | None = Field(default=None, repr=False)
    maximum_proxy_body_bytes: int = Field(default=10_485_760, ge=1, le=52_428_800)
    stream_queue_size: int = Field(default=512, ge=8, le=8192)
    stream_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    frontend_dist: Path = Path(__file__).resolve().parents[1] / "frontend" / "dist"

    @field_validator("internal_host")
    @classmethod
    def internal_listener_must_be_loopback(cls, value: str) -> str:
        candidate = value.strip().lower()
        if candidate == "localhost":
            return candidate
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise ValueError("internal stream host must be loopback") from exc
        if not address.is_loopback:
            raise ValueError("internal stream listener must bind to loopback")
        return candidate

    @field_validator("control_api_url")
    @classmethod
    def control_api_must_be_loopback_http(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Control API URL must be an absolute HTTP(S) URL")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if parsed.hostname.lower() != "localhost":
                raise ValueError("Control API must use a loopback host") from None
        else:
            if not address.is_loopback:
                raise ValueError("Control API must use a loopback host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Control API URL must not contain credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("allowed_hosts")
    @classmethod
    def hosts_must_be_explicit(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any("*" in host or not host.strip() for host in value):
            raise ValueError("Web/BFF allowed hosts must be explicit")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def origins_must_be_explicit(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("Web/BFF requires at least one allowed origin")
        for origin in value:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname is None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or "*" in origin
            ):
                raise ValueError("Web/BFF origins must be explicit HTTP(S) origins")
        return value

    @field_validator("frontend_dist")
    @classmethod
    def normalize_frontend_dist(cls, value: Path) -> Path:
        return value.expanduser().resolve()
