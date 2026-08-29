from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from misaka_service_runtime import ServiceDefinition, ServiceManager

from aitools_service_manager.config import ManagementConfig

CONTROL_PLANE_SERVICE_ID = "control-plane"
CODEX_APP_SERVER_SERVICE_ID = "codex-app-server"
COORDINATOR_SERVICE_ID = "multi-agent-coordinator"
TERMINAL_HOST_SERVICE_ID = "terminal-host"
MAIN_WEB_SERVICE_ID = "web-v3"


def create_local_service_manager(config: ManagementConfig) -> ServiceManager:
    os.environ["VITE_API_PROXY_TARGET"] = config.control_plane_url
    os.environ["VITE_COORDINATOR_API_PROXY_TARGET"] = config.coordinator_url
    os.environ["VITE_MANAGEMENT_API_PROXY_TARGET"] = config.management_url
    os.environ["VITE_TERMINAL_HOST_PROXY_TARGET"] = config.terminal_host_url

    node_executable = _node_executable()
    vite_entry = config.root / "multi-agent-web-v3" / "node_modules" / "vite" / "bin" / "vite.js"
    return ServiceManager(
        (
            ServiceDefinition(
                service_id=CODEX_APP_SERVER_SERVICE_ID,
                display_name="Codex App Server",
                description="统一承载 Codex Provider 结构化控制连接和 Remote TUI 观察连接。",
                category="Infrastructure",
                command=codex_app_server_command(config),
                working_directory=str(config.root / "multi-agent-v3"),
                endpoint=config.codex_app_server_url,
                health_url=config.codex_app_server_health_url,
                startup_timeout_seconds=30.0,
                shutdown_timeout_seconds=15.0,
            ),
            ServiceDefinition(
                service_id=CONTROL_PLANE_SERVICE_ID,
                display_name="Multi-Agent V3 Control Plane",
                description="由 AITools 持久化配置启动的 V3 API、任务编排和服务目录入口。",
                category="Core",
                command=control_plane_command(config),
                working_directory=str(config.root / "multi-agent-v3"),
                endpoint=config.control_plane_url,
                health_url=f"{config.control_plane_url}/ready",
                startup_timeout_seconds=30.0,
                shutdown_timeout_seconds=10.0,
            ),
            ServiceDefinition(
                service_id=COORDINATOR_SERVICE_ID,
                display_name="Multi-Agent Coordinator",
                description="Microsoft Agent Framework 驱动的持久化任务协调与委派入口。",
                category="Application",
                command=coordinator_command(config),
                working_directory=str(config.root / "multi-agent-coordinator"),
                endpoint=config.coordinator_url,
                health_url=f"{config.coordinator_url}/ready",
                startup_timeout_seconds=45.0,
                shutdown_timeout_seconds=15.0,
            ),
            ServiceDefinition(
                service_id=TERMINAL_HOST_SERVICE_ID,
                display_name="Agent Terminal Host",
                description="托管 Codex/Claude PTY、终端快照、重连和单一输入控制租约。",
                category="Infrastructure",
                command=terminal_host_command(config),
                working_directory=str(config.root / "multi-agent-terminal-host"),
                endpoint=config.terminal_host_url,
                health_url=f"{config.terminal_host_url}/ready",
                startup_timeout_seconds=30.0,
                shutdown_timeout_seconds=15.0,
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


def control_plane_command(config: ManagementConfig) -> tuple[str, ...]:
    configuration_path = config.configuration_path
    if configuration_path is None:
        raise ValueError("runtime configuration path was not resolved")
    return (
        sys.executable,
        "-m",
        "aitools_service_manager.control_plane_host",
        "--root",
        str(config.root),
        "--configuration-path",
        str(configuration_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(config.control_plane_port),
        "--codex-app-server-url",
        config.codex_app_server_url,
    )


def codex_app_server_command(config: ManagementConfig) -> tuple[str, ...]:
    configuration_path = config.configuration_path
    if configuration_path is None:
        raise ValueError("runtime configuration path was not resolved")
    return (
        sys.executable,
        "-m",
        "aitools_service_manager.codex_app_server_host",
        "--configuration-path",
        str(configuration_path),
        "--listen-url",
        config.codex_app_server_url,
    )


def coordinator_command(config: ManagementConfig) -> tuple[str, ...]:
    configuration_path = config.configuration_path
    if configuration_path is None:
        raise ValueError("runtime configuration path was not resolved")
    return (
        str(coordinator_python_path(config)),
        "-m",
        "aitools_service_manager.coordinator_host",
        "--configuration-path",
        str(configuration_path),
        "--state-path",
        str(config.coordinator_state_path),
        "--control-plane-url",
        config.control_plane_url,
        "--host",
        "127.0.0.1",
        "--port",
        str(config.coordinator_port),
    )


def terminal_host_command(config: ManagementConfig) -> tuple[str, ...]:
    configuration_path = config.configuration_path
    if configuration_path is None:
        raise ValueError("runtime configuration path was not resolved")
    return (
        _node_executable(),
        str(config.root / "multi-agent-terminal-host" / "src" / "main.ts"),
        "--configuration-path",
        str(configuration_path),
        "--state-path",
        str(config.terminal_host_state_path),
        "--auth-token-file",
        str(config.terminal_host_auth_token_path),
        "--allowed-origin",
        config.main_web_url,
        "--allowed-origin",
        config.service_web_url,
        "--codex-remote-url",
        config.codex_app_server_url,
        "--host",
        "127.0.0.1",
        "--port",
        str(config.terminal_host_port),
    )


def coordinator_python_path(config: ManagementConfig) -> Path:
    return config.root / "multi-agent-coordinator" / ".venv" / "Scripts" / "python.exe"


def _node_executable() -> str:
    executable = shutil.which("node.exe") or shutil.which("node")
    if executable is None:
        raise RuntimeError("Node.js was not found on PATH")
    return executable
