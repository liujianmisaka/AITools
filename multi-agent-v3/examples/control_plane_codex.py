from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn
from misaka_codex_provider import CodexAgentProvider, CodexProviderConfig
from misaka_control_plane import (
    ControlPlaneService,
    WorkingDirectoryPolicy,
    create_app,
    create_local_service_manager,
)
from misaka_control_plane_workflow import create_dag_runner
from misaka_invocation_runtime import InvocationRuntime
from misaka_session_capability import MemorySessionStore


def _create_provider(
    *,
    provider_id: str,
    codex_home: Path,
    network_deny_enforced: bool,
) -> CodexAgentProvider:
    return CodexAgentProvider(
        CodexProviderConfig(
            provider_id=provider_id,
            codex_home=codex_home,
            network_deny_enforced=network_deny_enforced,
        ),
        session_store=MemorySessionStore(),
    )


def build_app(
    *,
    codex_home: Path,
    allowed_path_roots: tuple[Path, ...] = (),
    state_path: Path,
    provider_id: str,
    network_deny_enforced: bool,
    a2a_node_port: int = 8025,
    a2a_agent_host_port: int = 8026,
):
    runtime = InvocationRuntime()
    provider = _create_provider(
        provider_id=provider_id,
        codex_home=codex_home,
        network_deny_enforced=network_deny_enforced,
    )

    async def register_codex(target: InvocationRuntime) -> None:
        if not target.descriptors():
            await target.register_provider(provider_id, provider)

    service = ControlPlaneService(
        runtime,
        state_path=state_path,
        provider_setup=register_codex,
        dag_runner=create_dag_runner(runtime),
        cwd_policy=WorkingDirectoryPolicy(allowed_path_roots),
        service_manager=create_local_service_manager(
            project_root=Path(__file__).resolve().parents[1],
            python_executable=sys.executable,
            a2a_node_port=a2a_node_port,
            a2a_agent_host_port=a2a_agent_host_port,
        ),
    )
    return create_app(service)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Codex Control Plane profile")
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument(
        "--allowed-path-root",
        type=Path,
        action="append",
        default=[],
        help="Optional absolute directory root accepted by the path filter; repeatable",
    )
    parser.add_argument("--state-path", type=Path, default=Path(".data/control-plane-codex.jsonl"))
    parser.add_argument("--provider-id", default="codex")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8017)
    parser.add_argument(
        "--network-deny-enforced",
        action="store_true",
        help="Declare that the host enforces network deny for requests that omit allow",
    )
    parser.add_argument("--a2a-node-port", type=int, default=8025)
    parser.add_argument("--a2a-agent-host-port", type=int, default=8026)
    args = parser.parse_args()
    app = build_app(
        codex_home=args.codex_home.resolve(),
        allowed_path_roots=tuple(args.allowed_path_root),
        state_path=args.state_path,
        provider_id=args.provider_id,
        network_deny_enforced=args.network_deny_enforced,
        a2a_node_port=args.a2a_node_port,
        a2a_agent_host_port=args.a2a_agent_host_port,
    )
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
