from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_codex_provider import CodexAgentProvider, CodexProviderConfig
from misaka_invocation_contracts import CompletionBoundary, InvocationRequest
from misaka_invocation_runtime import InvocationRuntime
from misaka_kernel_contracts import JsonObject

OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one real Codex Provider invocation")
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument(
        "--prompt",
        default='Return only this JSON object: {"answer":"4"}',
    )
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--rpc-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--ephemeral", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.timeout_seconds <= 0 or args.rpc_timeout_seconds <= 0:
        raise ValueError("timeout values must be positive")
    cwd = args.cwd.expanduser().resolve()
    provider = CodexAgentProvider(
        CodexProviderConfig(
            codex_home=(args.codex_home.expanduser().resolve() if args.codex_home else None),
            workspace_roots=(cwd,),
            network_deny_enforced=not args.allow_network,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
            new_sessions_ephemeral=args.ephemeral,
        )
    )
    runtime = InvocationRuntime(
        provider_start_timeout_seconds=args.rpc_timeout_seconds * 3 + 5,
        cancellation_timeout_seconds=5.0,
        shutdown_timeout_seconds=10.0,
    )
    await runtime.register_provider("codex", provider)
    try:
        handle = await runtime.submit(
            InvocationRequest(
                invocation_id="codex-smoke",
                capability_id=AGENT_CAPABILITY_ID,
                operation=AGENT_OPERATION_INVOKE,
                input={
                    "prompt": args.prompt,
                    "cwd": str(cwd),
                    "sandbox": "read_only",
                },
                idempotency_key="codex-smoke",
                completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
                output_schema=OUTPUT_SCHEMA,
                policy_context={"network_policy": "allow" if args.allow_network else "deny"},
                model=args.model,
                effort=args.effort,
            ),
            provider_id="codex",
        )
        try:
            async with asyncio.timeout(args.timeout_seconds):
                result = await handle.wait()
        except TimeoutError:
            await handle.cancel("Codex smoke test deadline expired")
            result = await handle.wait()
        print(
            json.dumps(
                {
                    "status": result.status.value,
                    "output": result.output,
                    "errorCode": result.error_code,
                    "errorMessage": result.error_message,
                },
                ensure_ascii=False,
            )
        )
        return 0 if result.status.value == "succeeded" else 1
    finally:
        await runtime.stop()


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
