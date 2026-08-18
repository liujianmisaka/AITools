from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from misaka_codex_provider import CodexAgentProvider, CodexProviderConfig
from misaka_control_plane import ControlPlaneService, create_app
from misaka_control_plane_workflow import create_dag_runner
from misaka_invocation_runtime import InvocationRuntime


def build_app(
    *,
    codex_home: Path,
    workspace_roots: tuple[Path, ...],
    state_path: Path,
    provider_id: str,
    network_deny_enforced: bool,
):
    runtime = InvocationRuntime()
    provider = CodexAgentProvider(
        CodexProviderConfig(
            provider_id=provider_id,
            codex_home=codex_home,
            workspace_roots=workspace_roots,
            network_deny_enforced=network_deny_enforced,
        )
    )

    async def register_codex(target: InvocationRuntime) -> None:
        if not target.descriptors():
            await target.register_provider(provider_id, provider)

    service = ControlPlaneService(
        runtime,
        state_path=state_path,
        provider_setup=register_codex,
        dag_runner=create_dag_runner(runtime),
    )
    return create_app(service)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Codex Control Plane profile")
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, action="append", required=True)
    parser.add_argument("--state-path", type=Path, default=Path(".data/control-plane-codex.jsonl"))
    parser.add_argument("--provider-id", default="codex")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8017)
    parser.add_argument(
        "--network-deny-enforced",
        action="store_true",
        help="Declare that the host enforces network deny for requests that omit allow",
    )
    args = parser.parse_args()
    roots = tuple(path.resolve() for path in args.workspace_root)
    app = build_app(
        codex_home=args.codex_home.resolve(),
        workspace_roots=roots,
        state_path=args.state_path,
        provider_id=args.provider_id,
        network_deny_enforced=args.network_deny_enforced,
    )
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
