from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

from aitools_service_manager.config import RuntimeConfigurationStore


def coordinator_arguments(
    *,
    configuration_path: Path,
    state_path: Path,
    control_plane_url: str,
    host: str,
    port: int,
) -> list[str]:
    configuration = RuntimeConfigurationStore(configuration_path).load()
    arguments = [
        "misaka_coordinator_service.transport",
        "--transport",
        "http",
        "--host",
        host,
        "--port",
        str(port),
        "--control-plane-url",
        control_plane_url,
        "--state-path",
        str(state_path.expanduser().resolve()),
        "--model",
        configuration.coordinator_model,
        "--reasoning-effort",
        configuration.coordinator_reasoning_effort,
        "--api-key-env",
        configuration.coordinator_api_key_env,
        "--max-decision-steps",
        str(configuration.coordinator_max_decision_steps),
        "--wait-timeout-ms",
        str(configuration.coordinator_wait_timeout_ms),
        "--max-concurrent-delegations",
        str(configuration.coordinator_max_concurrent_delegations),
        "--max-total-delegations",
        str(configuration.coordinator_max_total_delegations),
        "--max-delegation-depth",
        str(configuration.coordinator_max_delegation_depth),
        "--max-plan-revisions",
        str(configuration.coordinator_max_plan_revisions),
        "--max-retries-per-node",
        str(configuration.coordinator_max_retries_per_node),
        "--max-runtime-minutes",
        str(configuration.coordinator_max_runtime_minutes),
        "--max-model-activations",
        str(configuration.coordinator_max_model_activations),
    ]
    for provider in configuration.providers:
        arguments.extend(("--allowed-provider-id", provider.provider_id))
    for root in configuration.allowed_path_roots:
        arguments.extend(("--allowed-workspace-root", str(root)))
    if configuration.coordinator_base_url is not None:
        arguments.extend(("--base-url", configuration.coordinator_base_url))
    return arguments


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Coordinator from AITools persisted runtime configuration"
    )
    parser.add_argument("--configuration-path", type=Path, required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--control-plane-url", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()
    sys.argv = coordinator_arguments(
        configuration_path=args.configuration_path,
        state_path=args.state_path,
        control_plane_url=args.control_plane_url,
        host=args.host,
        port=args.port,
    )
    runpy.run_module("misaka_coordinator_service.transport", run_name="__main__")


if __name__ == "__main__":
    main()
