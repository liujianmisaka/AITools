from __future__ import annotations

import sys
from pathlib import Path

from misaka_service_runtime import ServiceDefinition, ServiceManager


def create_local_service_manager(
    *,
    project_root: Path | None = None,
    python_executable: str | None = None,
    a2a_node_port: int = 8025,
    a2a_agent_host_port: int = 8026,
) -> ServiceManager:
    if not 1 <= a2a_node_port <= 65535 or not 1 <= a2a_agent_host_port <= 65535:
        raise ValueError("service ports must be between 1 and 65535")
    if a2a_node_port == a2a_agent_host_port:
        raise ValueError("service ports must be distinct")
    root = (project_root or Path(__file__).resolve().parents[4]).resolve()
    executable = python_executable or sys.executable
    return ServiceManager(
        (
            ServiceDefinition(
                service_id="a2a-node",
                display_name="Standalone A2A Node",
                description="独立的 A2A 协议节点; 使用 Fake Agent 提供本地测试能力。",
                category="A2A",
                command=(
                    executable,
                    "-m",
                    "misaka_a2a_node",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(a2a_node_port),
                ),
                working_directory=str(root),
                endpoint=f"http://127.0.0.1:{a2a_node_port}",
                health_url=f"http://127.0.0.1:{a2a_node_port}/health",
            ),
            ServiceDefinition(
                service_id="a2a-agent-host",
                display_name="A2A Agent Host",
                description="通过 A2A 发布本地 Agent Host; 使用 Fake Agent 进行验收。",
                category="Agent",
                command=(
                    executable,
                    "-m",
                    "misaka_a2a_agent_host",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(a2a_agent_host_port),
                ),
                working_directory=str(root),
                endpoint=f"http://127.0.0.1:{a2a_agent_host_port}",
                health_url=f"http://127.0.0.1:{a2a_agent_host_port}/health",
            ),
        )
    )
