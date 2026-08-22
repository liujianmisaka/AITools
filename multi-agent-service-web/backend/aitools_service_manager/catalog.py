from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from misaka_service_runtime import ServiceDefinition, ServiceManager

from aitools_service_manager.config import ManagementConfig

CONTROL_PLANE_SERVICE_ID = "control-plane"
MAIN_WEB_SERVICE_ID = "web-v3"


def create_local_service_manager(config: ManagementConfig) -> ServiceManager:
    state_path = config.state_path
    if state_path is None:
        raise ValueError("state path was not resolved")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["VITE_API_PROXY_TARGET"] = config.control_plane_url

    node_executable = _node_executable()
    vite_entry = config.root / "multi-agent-web-v3" / "node_modules" / "vite" / "bin" / "vite.js"
    return ServiceManager(
        (
            ServiceDefinition(
                service_id=CONTROL_PLANE_SERVICE_ID,
                display_name="Multi-Agent V3 Control Plane",
                description=(
                    f"V3 {config.profile.upper()} Profile 的本地 API、任务编排和服务目录入口。"
                ),
                category="Core",
                command=_control_plane_command(config, state_path),
                working_directory=str(config.root / "multi-agent-v3"),
                endpoint=config.control_plane_url,
                health_url=f"{config.control_plane_url}/ready",
                startup_timeout_seconds=30.0,
                shutdown_timeout_seconds=10.0,
            ),
            ServiceDefinition(
                service_id=MAIN_WEB_SERVICE_ID,
                display_name="Multi-Agent Web V3",
                description="执行、委派、能力、服务和决策的 V3 主控制台。",
                category="Application",
                command=(
                    node_executable,
                    str(vite_entry),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(config.main_web_port),
                    "--strictPort",
                ),
                working_directory=str(config.root / "multi-agent-web-v3"),
                endpoint=config.main_web_url,
                health_url=f"{config.main_web_url}/@vite/client",
                startup_timeout_seconds=20.0,
                shutdown_timeout_seconds=8.0,
            ),
        )
    )


def _control_plane_command(config: ManagementConfig, state_path: Path) -> tuple[str, ...]:
    if config.profile == "fake":
        return (
            sys.executable,
            "examples/control_plane_fake.py",
            "--host",
            "127.0.0.1",
            "--port",
            str(config.control_plane_port),
            "--state-path",
            str(state_path),
        )

    codex_home = config.codex_home
    if codex_home is None:
        raise ValueError("codex home is required for the codex profile")
    command = [
        sys.executable,
        "examples/control_plane_codex.py",
        "--host",
        "127.0.0.1",
        "--port",
        str(config.control_plane_port),
        "--codex-home",
        str(codex_home),
        "--state-path",
        str(state_path),
        "--provider-id",
        config.provider_id,
    ]
    for workspace_root in config.workspace_roots:
        command.extend(("--workspace-root", str(workspace_root)))
    for workspace_id in config.resolved_workspace_ids:
        command.extend(("--workspace-id", workspace_id))
    if config.network_deny_enforced:
        command.append("--network-deny-enforced")
    return tuple(command)


def _node_executable() -> str:
    executable = shutil.which("node.exe") or shutil.which("node")
    if executable is None:
        raise RuntimeError("Node.js was not found on PATH")
    return executable
