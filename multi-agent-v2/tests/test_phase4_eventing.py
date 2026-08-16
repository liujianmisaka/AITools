from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from multi_agent_v2.packages.control_plane.models import (
    GitRefTarget,
    ScheduleRecord,
)
from multi_agent_v2.packages.control_plane.schedule_adapter import (
    ScheduleContractError,
    build_temporal_schedule,
)
from multi_agent_v2.packages.domain.events import EventIngestResult
from multi_agent_v2.packages.eventing import (
    CloudEventEnvelope,
    CloudEventParseError,
    GitRefPoller,
    WebhookPolicy,
    WebhookVerificationError,
    generic_webhook_event,
    parse_http_cloud_event,
)
from multi_agent_v2.packages.persistence import (
    ConnectorAdvance,
    ConnectorCheckpointState,
    ControlPlaneRepository,
    RevisionConflict,
)
from multi_agent_v2.packages.policy import WorkspaceDefinition, WorkspaceRegistry


class _ConnectorRepository:
    def __init__(self) -> None:
        self.checkpoint: ConnectorCheckpointState | None = None
        self.events: list[str] = []

    async def get_connector_checkpoint(
        self,
        connector_id: str,
    ) -> ConnectorCheckpointState | None:
        assert connector_id == "repo-main"
        return self.checkpoint

    async def advance_connector_checkpoint(
        self,
        *,
        connector_id: str,
        connector_kind: str,
        configuration_hash: str,
        checkpoint_value: str,
        expected_previous: str | None,
    ) -> ConnectorAdvance:
        previous = self.checkpoint.checkpoint_value if self.checkpoint else None
        if previous != expected_previous:
            raise RevisionConflict("checkpoint changed")
        revision = (self.checkpoint.revision + 1) if self.checkpoint else 1
        self.checkpoint = ConnectorCheckpointState(
            connector_id=connector_id,
            connector_kind=connector_kind,
            configuration_hash=configuration_hash,
            checkpoint_value=checkpoint_value,
            revision=revision,
        )
        return ConnectorAdvance(
            initialized=previous is None,
            changed=previous is not None and previous != checkpoint_value,
            previous_value=previous,
            current_value=checkpoint_value,
            revision=revision,
        )

    async def ingest_event(self, event: CloudEventEnvelope) -> EventIngestResult:
        identifier = event.id
        duplicate = identifier in self.events
        if not duplicate:
            self.events.append(identifier)
        return EventIngestResult(inbox_id=identifier, duplicate=duplicate)


def test_structured_and_binary_cloudevents_are_normalized() -> None:
    structured = parse_http_cloud_event(
        {"content-type": "application/cloudevents+json"},
        json.dumps(
            {
                "specversion": "1.0",
                "id": "event-1",
                "source": "urn:test",
                "type": "dev.misaka.test.v1",
                "subject": "main",
                "data": {"value": 3},
            }
        ).encode(),
    )
    binary = parse_http_cloud_event(
        {
            "content-type": "application/json",
            "ce-specversion": "1.0",
            "ce-id": "event-2",
            "ce-source": "urn:test",
            "ce-type": "dev.misaka.test.v1",
        },
        b'{"value":4}',
    )

    assert structured.data == {"value": 3}
    assert binary.data == {"value": 4}
    assert binary.datacontenttype == "application/json"


def test_cloudevent_rejects_non_object_data() -> None:
    with pytest.raises(CloudEventParseError):
        parse_http_cloud_event(
            {"content-type": "application/cloudevents+json"},
            b'{"specversion":"1.0","id":"1","source":"urn:test","type":"x","data":[]}',
        )


def test_webhook_hmac_timestamp_and_deterministic_fallback_id() -> None:
    body = b'{"answer":42}'
    timestamp = "1000"
    source_name = "build"
    nonce = "delivery-001"
    secret = b"test-secret"
    signature = hmac.new(
        secret,
        (timestamp.encode() + b"\n" + source_name.encode() + b"\n" + nonce.encode() + b"\n" + body),
        hashlib.sha256,
    ).hexdigest()
    policy = WebhookPolicy(
        secret=secret,
        clock=lambda: 1000.0,
        timestamp_tolerance_seconds=30,
    )
    headers = {
        "X-Misaka-Timestamp": timestamp,
        "X-Misaka-Signature": f"sha256={signature}",
        "X-Misaka-Nonce": nonce,
    }

    policy.verify(headers, body, source_name=source_name)
    first = generic_webhook_event(source_name=source_name, headers=headers, body=body)
    second = generic_webhook_event(source_name=source_name, headers=headers, body=body)

    assert first.id == second.id == nonce
    assert first.data == {"answer": 42}
    with pytest.raises(WebhookVerificationError):
        policy.verify(
            {**headers, "X-Misaka-Timestamp": "900"},
            body,
            source_name=source_name,
        )
    with pytest.raises(WebhookVerificationError):
        policy.verify(headers, body, source_name="other-source")


def test_temporal_schedule_adapter_supports_cron_interval_and_rejects_bad_target() -> None:
    now = datetime.now(UTC)
    cron = ScheduleRecord(
        schedule_id="nightly",
        name="Nightly",
        revision=1,
        enabled=True,
        schedule_kind="cron",
        schedule_spec={"expressions": ["0 1 * * *"], "timeZone": "Asia/Shanghai"},
        target_kind="workflow",
        target={
            "templateId": "addition",
            "templateVersion": 1,
            "workflowInput": {"formula": "1 + 2"},
        },
        created_at=now,
        updated_at=now,
    )
    interval = cron.model_copy(
        update={
            "schedule_id": "poll-main",
            "schedule_kind": "interval",
            "schedule_spec": {"everySeconds": 60, "offsetSeconds": 5},
            "target_kind": "git_connector",
            "target": {
                "connectorId": "repo-main",
                "workspaceId": "repo",
                "remote": "origin",
                "branch": "main",
            },
        }
    )

    assert build_temporal_schedule(cron).spec.cron_expressions == ("0 1 * * *",)
    assert build_temporal_schedule(interval).spec.intervals[0].every.total_seconds() == 60
    with pytest.raises(ScheduleContractError):
        build_temporal_schedule(cron.model_copy(update={"target": {"templateId": "addition"}}))


async def test_git_ref_poller_detects_a_new_commit_on_the_configured_branch(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", str(repository))
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    (repository / "value.txt").write_text("one", encoding="utf-8")
    _git(repository, "add", "value.txt")
    _git(repository, "commit", "-m", "initial")
    _git(repository, "branch", "-M", "main")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")

    fake = _ConnectorRepository()
    poller = GitRefPoller(
        repository=cast(ControlPlaneRepository, fake),
        workspaces=WorkspaceRegistry(
            [
                WorkspaceDefinition(
                    workspace_id="repo",
                    root=repository,
                    worktree_root=worktrees,
                )
            ]
        ),
    )
    target = GitRefTarget(
        connector_id="repo-main",
        workspace_id="repo",
        branch="main",
    )

    initial = await poller.poll(target)
    (repository / "value.txt").write_text("two", encoding="utf-8")
    _git(repository, "commit", "-am", "update")
    _git(repository, "push", "origin", "main")
    changed = await poller.poll(target)

    assert initial.initialized is True
    assert initial.event is None
    assert changed.changed is True
    assert changed.previous_commit != changed.current_commit
    assert changed.event is not None
    assert changed.event.type == "dev.misaka.git.commit.updated.v1"
    assert fake.events == [changed.event.id]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
