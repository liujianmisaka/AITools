from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from misaka_event_source import (
    CloudEvent,
    CronSchedule,
    GitBranchPoller,
    GitPollerConfig,
    MemoryEventSource,
    TimerEventSource,
    TimerSchedule,
    WebhookConfig,
    WebhookEventSource,
)


@pytest.mark.asyncio
async def test_memory_event_source_is_idempotent_replayable_and_bounded() -> None:
    source = MemoryEventSource(max_events=2)
    first = await source.publish_data(
        event_id="event-1",
        source="test",
        event_type="test.created.v1",
        data={"value": 1},
    )
    assert await source.publish(first) == first
    await source.publish_data(
        event_id="event-2",
        source="test",
        event_type="test.created.v1",
        data={"value": 2},
    )
    with pytest.raises(RuntimeError, match="capacity"):
        await source.publish_data(
            event_id="event-3",
            source="test",
            event_type="test.created.v1",
            data={"value": 3},
        )
    await source.close()
    events = [event async for event in source.events(start_sequence=1)]
    assert [event.event_id for event in events] == ["event-1", "event-2"]


@pytest.mark.asyncio
async def test_webhook_source_verifies_hmac_and_normalizes_cloud_events() -> None:
    secret = b"secret"
    config = WebhookConfig(
        source="webhook.test",
        event_type="dev.webhook.received.v1",
        secret=secret,
    )
    source = WebhookEventSource(config)
    body = json.dumps({"value": 1}).encode()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    event = await source.ingest(
        event_id="webhook-1",
        body=body,
        headers={"X-Signature-256": signature},
    )
    assert event.source == "webhook.test"
    assert event.event_type == "dev.webhook.received.v1"
    assert event.data == {"value": 1}
    with pytest.raises(ValueError, match="signature"):
        await source.ingest(event_id="webhook-2", body=body, headers={"x-signature-256": "bad"})

    structured = json.dumps(
        {
            "specversion": "1.0",
            "id": "cloud-1",
            "source": "remote.test",
            "type": "dev.remote.changed.v1",
            "data": {"ok": True},
        }
    ).encode()
    cloud_event = await source.ingest(
        event_id="ignored",
        body=structured,
        headers={
            "x-signature-256": "sha256=" + hmac.new(secret, structured, hashlib.sha256).hexdigest()
        },
    )
    assert cloud_event.event_id == "cloud-1"
    assert cloud_event.event_type == "dev.remote.changed.v1"
    await source.close()


@pytest.mark.asyncio
async def test_timer_source_emits_deterministic_events_and_closes() -> None:
    source = TimerEventSource(
        TimerSchedule(
            interval_seconds=0.001,
            source="timer.test",
            event_type="dev.timer.tick.v1",
            max_occurrences=2,
            start_immediately=True,
        ),
        lambda count, now: {"count": count, "at": now.isoformat()},
    )
    await source.start()
    await asyncio.wait_for(source.wait(), timeout=1)
    await source.close()
    events = [event async for event in source.events(start_sequence=1)]
    assert [event.event_id for event in events] == ["timer:timer.test:1", "timer:timer.test:2"]


def test_cron_schedule_validates_and_calculates_next_occurrence() -> None:
    schedule = CronSchedule("*/5 * * * *")
    moment = datetime(2026, 8, 18, 12, 1, tzinfo=UTC)
    assert schedule.next_after(moment).minute == 5
    with pytest.raises(ValueError, match="invalid cron"):
        CronSchedule("not a cron")


@pytest.mark.asyncio
async def test_git_poller_emits_only_when_branch_head_changes(tmp_path: Path) -> None:
    commits = iter(("a" * 40, "a" * 40, "b" * 40))

    async def fake_rev_parse(repository: Path, branch: str) -> str:
        assert repository == tmp_path
        assert branch == "main"
        return next(commits)

    poller = GitBranchPoller(
        GitPollerConfig(repository=tmp_path, branch="main", emit_initial=False),
        rev_parse=fake_rev_parse,
    )
    assert await poller.poll_once() is None
    assert await poller.poll_once() is None
    event = await poller.poll_once()
    assert event is not None
    assert event.data["commit"] == "b" * 40
    assert event.data["previous_commit"] == "a" * 40
    await poller.close()


def test_cloud_event_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone"):
        CloudEvent(
            event_id="event",
            source="test",
            event_type="test.v1",
            data={},
            occurred_at=datetime.now(),
        )
