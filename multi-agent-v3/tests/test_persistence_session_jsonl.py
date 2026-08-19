from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import (
    DurableConflict,
    DurableCorruption,
    DurableEvent,
    DurableNotFound,
    SessionHeader,
)
from misaka_persistence_jsonl import JsonlEventLog, JsonlSessionLog


class _ValueProjection:
    def __init__(self) -> None:
        self.values: list[str] = []

    async def reset(self) -> None:
        self.values.clear()

    async def apply(self, event: DurableEvent) -> None:
        value = event.payload.get("value")
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        self.values.append(value)

    def snapshot(self) -> tuple[str, ...]:
        return tuple(self.values)


def _header(session_id: str = "session-1") -> SessionHeader:
    return SessionHeader(
        session_id=session_id,
        owner_id="principal-1",
        scope_id="scope-1",
        composition_id="profile.agent-host",
        metadata={"purpose": "test"},
        created_at=datetime(2026, 8, 19, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_jsonl_session_log_persists_header_facts_and_projection_checkpoint(
    tmp_path: Path,
) -> None:
    event_log = JsonlEventLog(tmp_path / "session.jsonl")
    sessions = JsonlSessionLog(event_log)
    header = _header()

    assert await sessions.create(header) == header
    assert await sessions.create(header) == header
    with pytest.raises(DurableConflict):
        await sessions.create(
            SessionHeader(
                session_id=header.session_id,
                owner_id=header.owner_id,
                scope_id=header.scope_id,
                composition_id=header.composition_id,
                metadata={"purpose": "different"},
                created_at=header.created_at,
            )
        )
    with pytest.raises(DurableNotFound):
        await sessions.append("missing", "fact-0", "value.added", {"value": "x"})
    first = await sessions.append("session-1", "fact-1", "value.added", {"value": "a"})
    second = await sessions.append("session-1", "fact-2", "value.added", {"value": "b"})
    assert (first.sequence, second.sequence) == (1, 2)

    inspection = await sessions.inspect("session-1")
    assert inspection.last_sequence == 2
    assert inspection.fact_count == 2
    assert await sessions.list() == (header,)

    projection = _ValueProjection()
    replay = await sessions.replay("session-1", projection)
    assert replay.state == ("a", "b")
    assert replay.checkpoint.stream_id == "session.facts:session-1"
    assert replay.checkpoint.source_sequence == 2
    assert replay.checkpoint.applied_sequence == 2

    await sessions.append("session-1", "fact-3", "value.added", {"value": "c"})
    incremental = await sessions.replay("session-1", projection, start_sequence=3, reset=False)
    assert incremental.state == ("a", "b", "c")
    assert incremental.checkpoint.source_sequence == 3
    assert incremental.checkpoint.applied_sequence == 3

    empty_projection = _ValueProjection()
    await sessions.create(_header("session-empty"))
    empty = await sessions.replay("session-empty", empty_projection)
    assert empty.state == ()
    assert empty.checkpoint.stream_id == "session.facts:session-empty"
    assert empty.checkpoint.source_sequence == 0
    assert empty.checkpoint.applied_sequence == 0

    reopened = JsonlSessionLog(JsonlEventLog(tmp_path / "session.jsonl"))
    assert await reopened.get("session-1") == header
    assert [event.event_id for event in await reopened.read("session-1")] == [
        "fact-1",
        "fact-2",
        "fact-3",
    ]


@pytest.mark.asyncio
async def test_jsonl_session_log_rejects_facts_without_a_header(tmp_path: Path) -> None:
    event_log = JsonlEventLog(tmp_path / "orphan.jsonl")
    await event_log.append("session.facts:orphan", "fact-1", "value.added", {"value": "x"})

    with pytest.raises(DurableCorruption, match="without headers"):
        await JsonlSessionLog(JsonlEventLog(tmp_path / "orphan.jsonl")).open()


@pytest.mark.asyncio
async def test_jsonl_event_log_rejects_unsupported_fact_schema(tmp_path: Path) -> None:
    path = tmp_path / "unsupported.jsonl"
    record: JsonObject = {
        "stream_id": "stream-1",
        "sequence": 1,
        "event_id": "event-1",
        "event_type": "test.created",
        "payload": {},
        "schema_version": 99,
        "occurred_at": "2026-08-19T00:00:00+00:00",
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(DurableCorruption, match="unsupported durable event schema"):
        await JsonlEventLog(path).open()
