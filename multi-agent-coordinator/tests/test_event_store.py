import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from misaka_coordinator_service.persistence import (
    CoordinatorEventStoreError,
    CoordinatorSessionEvent,
    JsonlCoordinatorEventStore,
)


def test_jsonl_coordinator_event_store_replays_and_restores(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.events.jsonl"
    first = JsonlCoordinatorEventStore(path)
    first.append(
        "session-1",
        "user.message",
        {"message": "hello"},
        occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
    )
    first.append("session-1", "activation.started", {"activation_id": "activation-1"})
    first.append("session-2", "user.message", {"message": "other"})
    first.close()

    restored = JsonlCoordinatorEventStore(path)
    assert [event.sequence for event in restored.list_events("session-1")] == [1, 2]
    assert restored.list_events("session-1", next_sequence=2)[0].event_type == "activation.started"
    assert restored.list_events("session-2")[0].payload == {"message": "other"}


def test_jsonl_coordinator_event_store_rejects_invalid_cursor(tmp_path: Path) -> None:
    store = JsonlCoordinatorEventStore()
    with pytest.raises(CoordinatorEventStoreError, match="positive"):
        store.list_events("session-1", next_sequence=0)
    with pytest.raises(CoordinatorEventStoreError, match="JSON values"):
        store.append("session-1", "invalid", {"value": object()})  # type: ignore[arg-type]


def test_jsonl_coordinator_event_store_streams_replayed_and_new_events() -> None:
    async def exercise() -> None:
        store = JsonlCoordinatorEventStore()
        store.append("session-1", "first", {})
        stream = cast(
            AsyncGenerator[CoordinatorSessionEvent, None],
            store.stream_events("session-1", next_sequence=1),
        )
        assert (await anext(stream)).sequence == 1

        next_event: asyncio.Future[CoordinatorSessionEvent] = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        store.append("session-1", "second", {})
        assert (await next_event).sequence == 2
        await stream.aclose()

    asyncio.run(exercise())
