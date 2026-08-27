from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from misaka_coordinator_service.application import CoordinatorReasoningEffort
from misaka_coordinator_service.transport.host import (
    CoordinatorHostConfig,
    create_http_application,
    create_mcp_server,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the persistent Microsoft Agent Framework Coordinator"
    )
    parser.add_argument("--transport", choices=("http", "mcp"), default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--control-plane-url", default="http://127.0.0.1:8016")
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path(".data/multi-agent-coordinator/sessions.jsonl"),
    )
    parser.add_argument("--model", default="pixel/gpt-5.6-luna")
    parser.add_argument(
        "--reasoning-effort",
        choices=tuple(effort.value for effort in CoordinatorReasoningEffort),
        default=CoordinatorReasoningEffort.MEDIUM.value,
    )
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--base-url")
    parser.add_argument("--max-decision-steps", type=int, default=16)
    parser.add_argument("--wait-timeout-ms", type=int, default=0)
    parser.add_argument("--mcp-request-timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    config = CoordinatorHostConfig(
        control_plane_url=args.control_plane_url,
        state_path=args.state_path,
        model=args.model,
        reasoning_effort=CoordinatorReasoningEffort(args.reasoning_effort),
        api_key_env=args.api_key_env,
        base_url=args.base_url,
        host=args.host,
        port=args.port,
        max_decision_steps=args.max_decision_steps,
        wait_timeout_ms=args.wait_timeout_ms,
        mcp_request_timeout_seconds=args.mcp_request_timeout_seconds,
    )
    if args.transport == "mcp":
        _runtime, server = create_mcp_server(config)
        server.run(transport="streamable-http")
        return
    _runtime, application = create_http_application(config)
    uvicorn.run(application, host=config.host, port=config.port, lifespan="on")


if __name__ == "__main__":
    main()
