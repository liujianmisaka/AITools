from __future__ import annotations

import asyncio

from misaka_invocation_runtime import InvocationRuntime
from temporalio import activity

from misaka_coordinator_temporal.contracts import TemporalInvocationInput, TemporalResultPayload
from misaka_coordinator_temporal.workflow import TEMPORAL_INVOCATION_ACTIVITY


class InvocationActivityRunner:
    def __init__(self, runtime: InvocationRuntime) -> None:
        self._runtime = runtime

    @activity.defn(name=TEMPORAL_INVOCATION_ACTIVITY)
    async def execute(self, input: TemporalInvocationInput) -> TemporalResultPayload:
        handle = await self._runtime.submit(input.to_request(), provider_id=input.provider_id)
        wait_task = asyncio.create_task(handle.wait())
        try:
            while True:
                done, _ = await asyncio.wait({wait_task}, timeout=input.heartbeat_interval_seconds)
                if done:
                    return TemporalResultPayload.from_result(await wait_task)
                activity.heartbeat(
                    {
                        "invocation_id": input.invocation_id,
                        "attempt": activity.info().attempt,
                    }
                )
        except asyncio.CancelledError:
            await handle.cancel("Temporal activity cancelled")
            await asyncio.gather(wait_task, return_exceptions=True)
            raise
        finally:
            if not wait_task.done():
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
