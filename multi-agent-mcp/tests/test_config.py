from __future__ import annotations

import pytest

from misaka_mcp_gateway.__main__ import build_parser
from misaka_mcp_gateway.config import GatewayConfig


def test_config_normalizes_control_plane_url() -> None:
    config = GatewayConfig(control_plane_url="http://127.0.0.1:8016/")
    assert config.control_plane_url == "http://127.0.0.1:8016"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("control_plane_url", "file:///tmp/control-plane"),
        ("actor_kind", "robot"),
        ("sandbox", "danger-full-access"),
        ("network_policy", "maybe"),
        ("timeout_seconds", 0),
    ),
)
def test_config_rejects_unsafe_or_empty_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        GatewayConfig(**{field: value})  # type: ignore[arg-type]


def test_parser_does_not_define_legacy_workspace_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MISAKA_WORKSPACE_ID", "legacy-workspace")

    args = build_parser().parse_args([])

    assert not hasattr(args, "workspace_id")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--workspace-id", "legacy-workspace"])
