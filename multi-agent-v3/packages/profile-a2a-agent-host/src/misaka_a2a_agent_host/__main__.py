from __future__ import annotations

import argparse

import uvicorn

from misaka_a2a_agent_host.node import A2AAgentHostConfig, create_fake_a2a_agent_host


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Multi-Agent V3 A2A Agent Host profile")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8016)
    parser.add_argument("--public-url")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    config = A2AAgentHostConfig(
        host=args.host,
        port=args.port,
        public_url=args.public_url,
    )
    node = create_fake_a2a_agent_host(config=config)
    uvicorn.run(node.app, host=config.host, port=config.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
