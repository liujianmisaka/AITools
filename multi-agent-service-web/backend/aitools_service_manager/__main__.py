from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from aitools_service_manager.app import create_app
from aitools_service_manager.catalog import create_local_service_manager
from aitools_service_manager.client import HttpControlPlaneClient
from aitools_service_manager.config import ManagementConfig
from aitools_service_manager.service import ManagementService


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AITools local service manager")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8014)
    parser.add_argument("--service-web-port", type=int, default=5174)
    parser.add_argument("--control-plane-port", type=int, default=8016)
    parser.add_argument("--main-web-port", type=int, default=5173)
    parser.add_argument("--coordinator-port", type=int, default=8020)
    parser.add_argument("--terminal-host-port", type=int, default=8022)
    parser.add_argument("--codex-app-server-port", type=int, default=8048)
    parser.add_argument("--configuration-path", type=Path)
    args = parser.parse_args()

    config = ManagementConfig(
        root=args.root,
        management_host=args.host,
        management_port=args.port,
        service_web_port=args.service_web_port,
        control_plane_port=args.control_plane_port,
        main_web_port=args.main_web_port,
        coordinator_port=args.coordinator_port,
        terminal_host_port=args.terminal_host_port,
        codex_app_server_port=args.codex_app_server_port,
        configuration_path=args.configuration_path,
    )
    service = ManagementService(
        config,
        create_local_service_manager(config),
        HttpControlPlaneClient(config.control_plane_url),
    )
    uvicorn.run(
        create_app(service),
        host=config.management_host,
        port=config.management_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
