from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn
from misaka_codex_provider import CodexAgentProvider, CodexProviderConfig
from misaka_control_plane import (
    ControlPlaneService,
    WorkspaceCatalog,
    create_app,
    create_local_service_manager,
)
from misaka_control_plane_workflow import create_dag_runner
from misaka_invocation_runtime import InvocationRuntime
from misaka_session_capability import MemorySessionStore


def _workspace_entries(
    workspace_roots: tuple[Path, ...],
    workspace_ids: tuple[str, ...] | None,
) -> dict[str, Path]:
    selected_ids = workspace_ids or tuple(
        f"workspace-{index}" for index in range(1, len(workspace_roots) + 1)
    )
    if len(selected_ids) != len(workspace_roots):
        raise ValueError("workspace ids must match workspace roots one-to-one")
    if any(not workspace_id.strip() for workspace_id in selected_ids):
        raise ValueError("workspace ids must not be empty")
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("workspace ids must be unique")
    return dict(zip(selected_ids, workspace_roots, strict=True))


def _create_provider(
    *,
    provider_id: str,
    codex_home: Path,
    workspace_roots: tuple[Path, ...],
    network_deny_enforced: bool,
) -> CodexAgentProvider:
    return CodexAgentProvider(
        CodexProviderConfig(
            provider_id=provider_id,
            codex_home=codex_home,
            workspace_roots=workspace_roots,
            network_deny_enforced=network_deny_enforced,
        ),
        session_store=MemorySessionStore(),
    )


def build_app(
    *,
    codex_home: Path,
    workspace_roots: tuple[Path, ...],
    workspace_ids: tuple[str, ...] | None = None,
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
        workspace_roots=workspace_roots,
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
        workspace_catalog=WorkspaceCatalog(_workspace_entries(workspace_roots, workspace_ids)),
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
    parser.add_argument("--workspace-root", type=Path, action="append", required=True)
    parser.add_argument(
        "--workspace-id",
        action="append",
        help=(
            "Opaque Delegation workspace ID for each --workspace-root; defaults to "
            "workspace-1, workspace-2, ..."
        ),
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
    roots = tuple(path.resolve() for path in args.workspace_root)
    workspace_ids = tuple(args.workspace_id) if args.workspace_id is not None else None
    app = build_app(
        codex_home=args.codex_home.resolve(),
        workspace_roots=roots,
        workspace_ids=workspace_ids,
        state_path=args.state_path,
        provider_id=args.provider_id,
        network_deny_enforced=args.network_deny_enforced,
        a2a_node_port=args.a2a_node_port,
        a2a_agent_host_port=args.a2a_agent_host_port,
    )
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
