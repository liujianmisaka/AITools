from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from misaka_control_plane import ControlPlaneService, create_app
from misaka_fake_agent import FakeAgentProvider
from misaka_invocation_runtime import InvocationRuntime


async def register_fake(runtime: InvocationRuntime) -> None:
    if not runtime.descriptors():
        await runtime.register_provider("fake", FakeAgentProvider())


runtime = InvocationRuntime()
service = ControlPlaneService(
    runtime,
    state_path=Path(__file__).resolve().parent / ".data" / "control-plane.jsonl",
    provider_setup=register_fake,
)
app = create_app(service)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local Fake Control Plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8016)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, reload=False)
