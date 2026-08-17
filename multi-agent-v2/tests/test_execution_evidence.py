from __future__ import annotations

from pathlib import Path
from typing import cast

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from multi_agent_v2.packages.persistence import (
    CURRENT_SCHEMA_REVISION,
    AgentExecutionEvent,
    ArtifactMetadata,
    EvidenceEventRegistration,
    derive_evidence_event_id,
)


def test_phase7_evidence_models_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    tables = (
        cast(Table, ArtifactMetadata.__table__),
        cast(Table, AgentExecutionEvent.__table__),
    )
    sql = "\n".join(str(CreateTable(table).compile(dialect=dialect)) for table in tables)
    index_sql = "\n".join(
        str(CreateIndex(index).compile(dialect=dialect))
        for table in tables
        for index in table.indexes
    )

    assert "CREATE TABLE artifacts" in sql
    assert "CREATE TABLE agent_execution_events" in sql
    assert "FOREIGN KEY(execution_id) REFERENCES agent_execution_leases" in sql
    assert "sha256 ~ '^[0-9a-f]{64}$'" in sql
    assert "JSONB" in sql
    assert "ix_agent_execution_event_execution" in index_sql
    assert "ix_artifact_execution" in index_sql


def test_phase7_migration_is_the_single_head() -> None:
    project_root = Path(__file__).parents[1]
    scripts = ScriptDirectory.from_config(Config(project_root / "alembic.ini"))
    revision = scripts.get_revision(CURRENT_SCHEMA_REVISION)

    assert revision is not None
    assert revision.down_revision == "0003_phase4_control_plane"
    assert scripts.get_heads() == [CURRENT_SCHEMA_REVISION]


def test_evidence_event_identity_is_deterministic_and_payload_sensitive() -> None:
    registration = EvidenceEventRegistration(
        execution_id="execution-1",
        attempt_id="attempt-1",
        event_type="turn_started",
        provider="fake",
        payload={"sequence": 1},
    )

    assert derive_evidence_event_id(registration) == derive_evidence_event_id(registration)
    assert derive_evidence_event_id(registration) != derive_evidence_event_id(
        EvidenceEventRegistration(
            execution_id="execution-1",
            attempt_id="attempt-1",
            event_type="turn_started",
            provider="fake",
            payload={"sequence": 2},
        )
    )
