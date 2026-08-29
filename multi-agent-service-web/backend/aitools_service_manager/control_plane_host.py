from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import cast

import uvicorn
from fastapi import FastAPI

from aitools_service_manager.config import (
    RuntimeConfigurationStore,
    apply_claude_runtime_environment,
    resolve_control_plane_state_path,
)
from aitools_service_manager.runtime_preflight import validate_provider_runtime_access

ControlPlaneBuilder = Callable[..., FastAPI]


def create_control_plane_app(
    *,
    root: Path,
    configuration_path: Path,
    codex_app_server_url: str | None = None,
) -> FastAPI:
    aitools_root = root.expanduser().resolve(strict=True)
    if not aitools_root.is_dir():
        raise ValueError(f"AITools root is not a directory: {root}")
    configuration = RuntimeConfigurationStore(configuration_path).load()
    validate_provider_runtime_access(configuration)
    apply_claude_runtime_environment(configuration)
    state_path = resolve_control_plane_state_path(aitools_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    entry_path = aitools_root / "multi-agent-v3" / "examples" / "control_plane_multi.py"
    builder = _profile_builder(entry_path)
    return builder(
        provider_configs=tuple(
            provider.to_profile_payload() for provider in configuration.providers
        ),
        allowed_path_roots=configuration.allowed_path_roots,
        state_path=state_path,
        codex_app_server_url=codex_app_server_url,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Control Plane from AITools persisted runtime configuration"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--configuration-path", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8016)
    parser.add_argument("--codex-app-server-url")
    args = parser.parse_args()
    app = create_control_plane_app(
        root=args.root,
        configuration_path=args.configuration_path,
        codex_app_server_url=args.codex_app_server_url,
    )
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


def _profile_builder(entry_path: Path) -> ControlPlaneBuilder:
    entry = run_path(str(entry_path))
    builder = entry.get("build_app")
    if not callable(builder):
        raise ValueError(f"Control Plane profile entry has no build_app: {entry_path}")
    return cast(ControlPlaneBuilder, builder)


if __name__ == "__main__":
    main()
