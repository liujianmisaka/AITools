from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from multi_agent_v2.apps.control_api.main import create_app
from multi_agent_v2.packages.config import Settings
from multi_agent_v2.packages.control_plane.models import (
    ProjectionEvent,
    TemplateCreate,
)
from multi_agent_v2.packages.domain.events import CloudEventEnvelope
from multi_agent_v2.packages.observability.health import HealthReport, ReadinessStatus
from multi_agent_v2.packages.persistence import (
    CURRENT_SCHEMA_REVISION,
    CommandOutbox,
    ControlPlaneRepository,
    DatabaseManager,
    DatabaseSchemaError,
    EventInbox,
    IdempotencyConflict,
    IdempotencyRecord,
    WorkflowEvent,
    WorkflowInstanceProjection,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("MULTI_AGENT_V2_RUN_INFRA_TESTS") != "1",
        reason="local infrastructure tests require an explicit opt-in",
    ),
]


@asynccontextmanager
async def _running_client(app: FastAPI) -> AsyncGenerator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


def _workspace_config(tmp_path: Path) -> Path:
    workspace_root = tmp_path / "workspace"
    worktree_root = tmp_path / "worktrees"
    workspace_root.mkdir()
    worktree_root.mkdir()
    path = tmp_path / "workspaces.json"
    path.write_text(
        json.dumps(
            {
                "workspaces": [
                    {
                        "id": "integration",
                        "root": str(workspace_root),
                        "worktreeRoot": str(worktree_root),
                        "baseRef": "HEAD",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


async def test_ready_with_real_local_dependencies(tmp_path: Path) -> None:
    database_url = os.environ["MULTI_AGENT_V2_DATABASE_URL"]
    temporal_address = os.environ["MULTI_AGENT_V2_TEMPORAL_ADDRESS"]
    settings = Settings(
        database_url=SecretStr(database_url),
        temporal_address=temporal_address,
        artifact_root=tmp_path / "artifacts",
        workspace_config_path=_workspace_config(tmp_path),
        dependency_timeout_seconds=5,
    )
    app = create_app(settings)

    async with _running_client(app) as client:
        response = await client.get("/ready")

    report = HealthReport.model_validate_json(response.content)
    assert response.status_code == 200
    assert report.status is ReadinessStatus.READY
    assert {component.name for component in report.components} == {
        "artifact_root",
        "postgresql",
        "temporal",
    }


async def test_database_probe_rejects_stale_alembic_revision() -> None:
    database_url = SecretStr(os.environ["MULTI_AGENT_V2_DATABASE_URL"])
    database = DatabaseManager(database_url)
    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = 'stale_revision'")
            )
        with pytest.raises(DatabaseSchemaError, match="revision"):
            await database.check()
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": CURRENT_SCHEMA_REVISION},
            )
        await database.close()


async def test_ready_rejects_unknown_temporal_namespace(tmp_path: Path) -> None:
    settings = Settings(
        database_url=SecretStr(os.environ["MULTI_AGENT_V2_DATABASE_URL"]),
        temporal_address=os.environ["MULTI_AGENT_V2_TEMPORAL_ADDRESS"],
        temporal_namespace="namespace-that-does-not-exist",
        artifact_root=tmp_path / "artifacts",
        workspace_config_path=_workspace_config(tmp_path),
        dependency_timeout_seconds=5,
    )
    app = create_app(settings)

    async with _running_client(app) as client:
        response = await client.get("/ready")

    report = HealthReport.model_validate_json(response.content)
    temporal = next(component for component in report.components if component.name == "temporal")
    assert response.status_code == 503
    assert report.status is ReadinessStatus.NOT_READY
    assert temporal.detail == "RPCError"


async def test_control_plane_idempotency_conflict_with_real_postgresql() -> None:
    database = DatabaseManager(SecretStr(os.environ["MULTI_AGENT_V2_DATABASE_URL"]))
    sessions = async_sessionmaker(database.engine, expire_on_commit=False)
    repository = ControlPlaneRepository(sessions)
    suffix = uuid4().hex
    template_id = f"t{suffix[:20]}"
    key = f"template-{suffix}"
    try:
        command = TemplateCreate(template_id=template_id, name="First")
        created = await repository.create_template(command, idempotency_key=key)
        replayed = await repository.create_template(command, idempotency_key=key)

        assert replayed == created
        with pytest.raises(IdempotencyConflict):
            await repository.create_template(
                command.model_copy(update={"name": "Different"}),
                idempotency_key=key,
            )
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.scope == "template.create",
                    IdempotencyRecord.idempotency_key == key,
                )
            )
            await session.execute(
                delete(WorkflowTemplate).where(WorkflowTemplate.template_id == template_id)
            )
        await database.close()


async def test_outbox_claim_and_fencing_with_real_postgresql() -> None:
    database = DatabaseManager(SecretStr(os.environ["MULTI_AGENT_V2_DATABASE_URL"]))
    sessions = async_sessionmaker(database.engine, expire_on_commit=False)
    repository = ControlPlaneRepository(sessions)
    suffix = uuid4().hex
    outbox_id = f"outbox-{suffix}"
    command_id = f"command-{suffix}"
    old_time = datetime(2000, 1, 1, tzinfo=UTC)
    try:
        async with sessions() as session, session.begin():
            session.add(
                CommandOutbox(
                    outbox_id=outbox_id,
                    command_id=command_id,
                    command_type="test.v1",
                    aggregate_type="test",
                    aggregate_id=suffix,
                    payload={},
                    status="pending",
                    attempts=0,
                    available_at=old_time,
                    lease_epoch=0,
                    created_at=old_time,
                    updated_at=old_time,
                )
            )

        claims = await asyncio.gather(
            repository.claim_outbox(
                lease_owner="integration-1",
                lease_duration=timedelta(seconds=30),
                limit=1,
            ),
            repository.claim_outbox(
                lease_owner="integration-2",
                lease_duration=timedelta(seconds=30),
                limit=1,
            ),
        )
        target_claims = [
            command for claimed in claims for command in claimed if command.command_id == command_id
        ]

        assert len(target_claims) == 1
        winner = target_claims[0]
        assert await repository.renew_outbox(
            winner,
            lease_duration=timedelta(seconds=30),
        )
        assert not await repository.renew_outbox(
            winner.model_copy(update={"lease_epoch": winner.lease_epoch + 1}),
            lease_duration=timedelta(seconds=30),
        )
        await repository.complete_outbox(winner)
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(CommandOutbox).where(CommandOutbox.outbox_id == outbox_id))
        await database.close()


async def test_event_inbox_deduplicates_with_real_postgresql() -> None:
    database = DatabaseManager(SecretStr(os.environ["MULTI_AGENT_V2_DATABASE_URL"]))
    sessions = async_sessionmaker(database.engine, expire_on_commit=False)
    repository = ControlPlaneRepository(sessions)
    suffix = uuid4().hex
    event = CloudEventEnvelope(
        id=suffix,
        source=f"urn:integration:{suffix}",
        type=f"dev.misaka.integration.{suffix}.v1",
        data={"value": 1},
    )
    inbox_id: str | None = None
    try:
        first = await repository.ingest_event(event)
        second = await repository.ingest_event(event)
        inbox_id = first.inbox_id

        assert first.duplicate is False
        assert second.duplicate is True
        assert second.inbox_id == first.inbox_id
    finally:
        if inbox_id is not None:
            async with sessions() as session, session.begin():
                await session.execute(delete(EventInbox).where(EventInbox.inbox_id == inbox_id))
        await database.close()


async def test_projection_does_not_regress_on_out_of_order_event() -> None:
    database = DatabaseManager(SecretStr(os.environ["MULTI_AGENT_V2_DATABASE_URL"]))
    sessions = async_sessionmaker(database.engine, expire_on_commit=False)
    repository = ControlPlaneRepository(sessions)
    suffix = uuid4().hex
    template_id = f"t{suffix[:20]}"
    instance_id = str(uuid4())
    workflow_id = f"multi-agent-v2/instances/{instance_id}"
    now = datetime.now(UTC)
    try:
        async with sessions() as session, session.begin():
            session.add(
                WorkflowTemplate(
                    template_id=template_id,
                    name="Projection test",
                    latest_version=1,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                WorkflowTemplateVersion(
                    template_id=template_id,
                    version=1,
                    definition={},
                    compiled_plan={},
                    plan_hash="a" * 64,
                    catalog_revision="integration",
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                WorkflowInstanceProjection(
                    instance_id=instance_id,
                    template_id=template_id,
                    template_version=1,
                    temporal_workflow_id=workflow_id,
                    status="pending_start",
                    workflow_input={},
                    projection_version=0,
                    created_at=now,
                    updated_at=now,
                )
            )

        def projection(version: int, state: str) -> ProjectionEvent:
            return ProjectionEvent(
                event_id=hashlib.sha256(f"{suffix}:{version}".encode()).hexdigest(),
                instance_id=instance_id,
                event_type="dev.misaka.workflow.snapshot.v1",
                occurred_at=now,
                data={
                    "schemaVersion": 1,
                    "temporalWorkflowId": workflow_id,
                    "temporalRunId": "run-1",
                    "status": state,
                    "projectionVersion": version,
                    "nodes": [],
                    "output": None,
                    "error": None,
                    "planHash": "a" * 64,
                },
            )

        assert await repository.publish_projection(projection(2, "running"))
        assert await repository.publish_projection(projection(1, "failed"))
        async with sessions() as session:
            row = await session.scalar(
                select(WorkflowInstanceProjection).where(
                    WorkflowInstanceProjection.instance_id == instance_id
                )
            )
            assert row is not None
            assert row.projection_version == 2
            assert row.status == "running"
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                delete(WorkflowEvent).where(WorkflowEvent.instance_id == instance_id)
            )
            await session.execute(
                delete(WorkflowInstanceProjection).where(
                    WorkflowInstanceProjection.instance_id == instance_id
                )
            )
            await session.execute(
                delete(WorkflowTemplateVersion).where(
                    WorkflowTemplateVersion.template_id == template_id
                )
            )
            await session.execute(
                delete(WorkflowTemplate).where(WorkflowTemplate.template_id == template_id)
            )
        await database.close()
