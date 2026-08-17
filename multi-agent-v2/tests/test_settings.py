from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from multi_agent_v2.packages.config import Settings


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.test", "::1"])
def test_control_api_rejects_non_loopback_bindings(host: str, tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(control_host=host, artifact_root=tmp_path)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_control_api_accepts_loopback_bindings(host: str, tmp_path: Path) -> None:
    settings = Settings(control_host=host, artifact_root=tmp_path)

    assert settings.control_host == host


def test_database_url_is_not_exposed_in_settings_repr(tmp_path: Path) -> None:
    secret = "postgresql+asyncpg://user:super-secret@127.0.0.1/database"

    settings = Settings(database_url=SecretStr(secret), artifact_root=tmp_path)

    assert "super-secret" not in repr(settings)
    assert settings.database_url.get_secret_value() == secret


def test_webhook_configuration_contains_only_a_credential_reference(tmp_path: Path) -> None:
    settings = Settings(
        webhook_secret_ref="Webhook.HMAC",
        credential_store_path=tmp_path / "credentials.json",
        artifact_root=tmp_path,
    )

    assert settings.webhook_secret_ref == "webhook.hmac"
    assert not hasattr(settings, "webhook_secret")


def test_control_api_rejects_wildcard_host_allowlist(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Wildcard"):
        Settings(allowed_hosts=("*",), artifact_root=tmp_path)


@pytest.mark.parametrize(
    "origin",
    ["*", "https://example.test/path", "file:///tmp", "https://user:pass@example.test"],
)
def test_control_api_rejects_invalid_origins(origin: str, tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Origins"):
        Settings(allowed_origins=(origin,), artifact_root=tmp_path)
