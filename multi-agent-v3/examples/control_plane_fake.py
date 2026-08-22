from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from misaka_control_plane import (
    ControlPlaneService,
    create_app,
    create_local_service_manager,
)
from misaka_control_plane_workflow import create_dag_runner
from misaka_fake_agent import FakeAgentProvider
from misaka_invocation_runtime import InvocationRuntime


async def register_fake(runtime: InvocationRuntime) -> None:
    if not runtime.descriptors():
        await runtime.register_provider("fake", FakeAgentProvider())


default_state_path = Path(__file__).resolve().parent / ".data" / "control-plane.jsonl"


def build_app(*, state_path: Path = default_state_path):
    runtime = InvocationRuntime()
    service = ControlPlaneService(
        runtime,
        state_path=state_path,
        provider_setup=register_fake,
        dag_runner=create_dag_runner(runtime),
        service_manager=create_local_service_manager(),
    )
    return create_app(service)


app = build_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local Fake Control Plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8016)
    parser.add_argument("--state-path", type=Path, default=default_state_path)
    args = parser.parse_args()
    uvicorn.run(
        build_app(state_path=args.state_path.resolve()),
        host=args.host,
        port=args.port,
        reload=False,
    )
