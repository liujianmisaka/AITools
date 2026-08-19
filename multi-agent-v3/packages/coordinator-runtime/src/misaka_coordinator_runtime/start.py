from __future__ import annotations

import asyncio

from misaka_coordinator_runtime.contracts import ExecutionHandle, ExecutionPlan


async def start_execution(
    plan: ExecutionPlan,
    *,
    attempt: int,
    cancellation_reason: str,
) -> ExecutionHandle:
    """Start a plan without losing the handle when the caller is cancelled."""

    if attempt < 1:
        raise ValueError("attempt must be at least one")
    if not cancellation_reason.strip():
        raise ValueError("cancellation_reason must not be empty")
    start_task = asyncio.create_task(plan.start(attempt=attempt))
    try:
        return await asyncio.shield(start_task)
    except asyncio.CancelledError as cancellation:
        try:
            handle = await asyncio.shield(start_task)
        except BaseException as startup_error:
            raise cancellation from startup_error
        try:
            await handle.cancel(cancellation_reason)
        except BaseException as cleanup_error:
            raise cancellation from cleanup_error
        raise cancellation
