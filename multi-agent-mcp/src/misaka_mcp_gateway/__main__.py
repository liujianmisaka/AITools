from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from typing import Any, TextIO, cast

from misaka_mcp_gateway.client import ControlPlaneClient
from misaka_mcp_gateway.config import GatewayConfig
from misaka_mcp_gateway.server import McpStdioServer


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value and value.strip() else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Expose Multi-Agent V3 task delegation through an MCP stdio server.")
    )
    parser.add_argument(
        "--control-plane-url",
        default=_env("MISAKA_CONTROL_PLANE_URL") or "http://127.0.0.1:8016",
    )
    parser.add_argument(
        "--provider-id",
        default=_env("MISAKA_PROVIDER_ID"),
        help="Default provider when delegate_task omits provider_id.",
    )
    parser.add_argument(
        "--model",
        default=_env("MISAKA_MODEL"),
        help="Default model when delegate_task omits model.",
    )
    parser.add_argument(
        "--effort",
        default=_env("MISAKA_EFFORT"),
        help="Default effort when delegate_task omits effort.",
    )
    parser.add_argument(
        "--actor-id",
        default=_env("MISAKA_ACTOR_ID") or "mcp-client",
    )
    parser.add_argument(
        "--actor-kind",
        default=_env("MISAKA_ACTOR_KIND") or "application",
    )
    parser.add_argument(
        "--scope-id",
        default=_env("MISAKA_SCOPE_ID") or "mcp",
    )
    parser.add_argument(
        "--sandbox",
        choices=("read_only", "workspace_write"),
        default=_env("MISAKA_SANDBOX") or "read_only",
    )
    parser.add_argument(
        "--network-policy",
        choices=("allow", "deny"),
        default=_env("MISAKA_NETWORK_POLICY") or "deny",
    )
    parser.add_argument(
        "--capability-id",
        default=_env("MISAKA_CAPABILITY_ID") or "agent.invocation",
    )
    parser.add_argument(
        "--operation",
        default=_env("MISAKA_OPERATION") or "invoke",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(_env("MISAKA_TIMEOUT_SECONDS") or "30"),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = GatewayConfig(
        control_plane_url=args.control_plane_url,
        provider_id=args.provider_id,
        model=args.model,
        effort=args.effort,
        actor_id=args.actor_id,
        actor_kind=args.actor_kind,
        scope_id=args.scope_id,
        sandbox=args.sandbox,
        network_policy=args.network_policy,
        capability_id=args.capability_id,
        operation=args.operation,
        timeout_seconds=args.timeout_seconds,
    )
    _configure_utf8(sys.stdin)
    _configure_utf8(sys.stdout)
    McpStdioServer(config, ControlPlaneClient(config)).run()


def _configure_utf8(stream: TextIO) -> None:
    reconfigure = cast(
        Callable[..., Any] | None,
        getattr(stream, "reconfigure", None),
    )
    if reconfigure is not None:
        reconfigure(encoding="utf-8")


if __name__ == "__main__":
    main()
