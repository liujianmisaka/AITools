from __future__ import annotations

import json
from pathlib import Path

import pytest
from misaka_persistence_contracts import DurableEvent, replay_events
from misaka_persistence_jsonl import (
    DurableConflict,
    DurableCorruption,
    DurableJobStatus,
    JsonlEventLog,
    JsonlJobRegistry,
)


class _EventProjection:
    def __init__(self) -> None:
        self.values: list[str] = []

    async def reset(self) -> None:
        self.values.clear()

    async def apply(self, event: DurableEvent) -> None:
        value = event.payload.get("value")
        if not isinstance(value, str):
            raise ValueError("projection event value must be a string")
        self.values.append(value)

    def snapshot(self) -> tuple[str, ...]:
        return tuple(self.values)


@pytest.mark.asyncio
async def test_jsonl_event_log_is_append_only_reopenable_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = JsonlEventLog(path)
    first = await log.append("stream-1", "event-1", "job.created", {"job_id": "job-1"})
    duplicate = await log.append("stream-1", "event-1", "job.created", {"job_id": "job-1"})
    second = await log.append("stream-1", "event-2", "job.updated", {"status": "running"})

    assert duplicate == first
    assert [event.sequence for event in await log.read("stream-1")] == [1, 2]
    assert second.sequence == 2

    reopened = JsonlEventLog(path)
    assert [event.event_id for event in await reopened.read("stream-1")] == ["event-1", "event-2"]

    with pytest.raises(DurableConflict):
        await reopened.append("stream-1", "event-1", "job.created", {"job_id": "different"})


@pytest.mark.asyncio
async def test_jsonl_event_log_rejects_corrupt_records(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("{not-json}\n", encoding="utf-8")
    with pytest.raises(DurableCorruption):
        await JsonlEventLog(path).open()


@pytest.mark.asyncio
async def test_jsonl_event_log_replays_durable_facts_into_a_projection(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "projection.jsonl")
    await log.append("stream-1", "event-1", "value.added", {"value": "a"})
    await log.append("stream-1", "event-2", "value.added", {"value": "b"})
    projection = _EventProjection()

    snapshot = await log.replay("stream-1", projection)
    repeated = await replay_events(await log.read("stream-1"), projection)

    assert snapshot == ("a", "b")
    assert repeated == snapshot

    incremental = _EventProjection()
    await log.replay("stream-1", incremental)
    incremental_snapshot = await log.replay("stream-1", incremental, start_sequence=2, reset=False)
    assert incremental_snapshot == ("a", "b", "b")


@pytest.mark.asyncio
async def test_job_registry_rebuilds_from_log_and_enforces_version_cas(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "jobs.jsonl")
    registry = JsonlJobRegistry(log)
    job, created = await registry.register("job-1", "idem-1", {"prompt": "hello"})
    assert created is True
    duplicate, duplicate_created = await registry.register("job-1", "idem-1", {"prompt": "hello"})
    assert duplicate == job
    assert duplicate_created is False

    running = await registry.transition(
        "job-1", DurableJobStatus.RUNNING, expected_version=job.version
    )
    assert running.version == 2
    with pytest.raises(DurableConflict):
        await registry.transition("job-1", DurableJobStatus.SUCCEEDED, expected_version=job.version)

    succeeded = await registry.transition(
        "job-1",
        DurableJobStatus.SUCCEEDED,
        expected_version=running.version,
        result={"answer": "ok"},
    )
    assert succeeded.status is DurableJobStatus.SUCCEEDED

    reopened_registry = JsonlJobRegistry(JsonlEventLog(tmp_path / "jobs.jsonl"))
    restored = await reopened_registry.get("job-1")
    assert restored == succeeded

    with pytest.raises(DurableConflict):
        await reopened_registry.transition("job-1", DurableJobStatus.FAILED)


@pytest.mark.asyncio
async def test_job_registry_preserves_json_values(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "jobs.jsonl")
    registry = JsonlJobRegistry(log)
    await registry.register(
        "job-json",
        "idem-json",
        {"prompt": "你好", "numbers": [1, 2], "enabled": True},
    )
    raw = json.loads((tmp_path / "jobs.jsonl").read_text(encoding="utf-8"))
    assert raw["payload"]["request"]["prompt"] == "你好"
    assert raw["payload"]["request"]["numbers"] == [1, 2]
