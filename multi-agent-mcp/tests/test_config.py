from __future__ import annotations

import pytest

from misaka_mcp_gateway.config import GatewayConfig


def test_config_normalizes_control_plane_url() -> None:
    config = GatewayConfig(control_plane_url="http://127.0.0.1:8016/")
    assert config.control_plane_url == "http://127.0.0.1:8016"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("control_plane_url", "file:///tmp/control-plane"),
        ("workspace_id", ""),
        ("actor_kind", "robot"),
        ("sandbox", "danger-full-access"),
        ("network_policy", "maybe"),
        ("timeout_seconds", 0),
    ),
)
def test_config_rejects_unsafe_or_empty_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        GatewayConfig(**{field: value})  # type: ignore[arg-type]
