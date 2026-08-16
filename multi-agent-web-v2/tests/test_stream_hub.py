from __future__ import annotations

import asyncio

from multi_agent_web_v2.stream_hub import StreamHub, TokenBatch, TokenEvent


def _event(sequence: int, *, execution_id: str = "execution-1") -> TokenEvent:
    return TokenEvent(
        execution_id=execution_id,
        sequence=sequence,
        kind="text_delta",
        text=f"token-{sequence}",
    )


async def test_stream_hub_deduplicates_by_execution_and_sequence() -> None:
    hub = StreamHub(queue_size=8)
    async with hub.subscribe() as queue:
        accepted = await hub.publish(TokenBatch(events=(_event(1), _event(1), _event(2))))

        assert accepted == 2
        assert (await queue.get()).sequence == 1
        assert (await queue.get()).sequence == 2
        assert queue.empty()


async def test_stream_hub_is_bounded_and_reports_dropped_tokens() -> None:
    hub = StreamHub(queue_size=2)
    async with hub.subscribe() as queue:
        await hub.publish(TokenBatch(events=(_event(1), _event(2), _event(3))))

        assert (await queue.get()).sequence == 2
        assert (await queue.get()).sequence == 3
        status = await hub.status()

    assert status.dropped_events == 1
    assert status.subscribers == 1
    await asyncio.sleep(0)
    assert (await hub.status()).subscribers == 0
