from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from multi_agent_v2.packages.persistence import (
    CURRENT_SCHEMA_REVISION,
    AgentExecutionAttempt,
    AgentExecutionLease,
    CleanupClaimDisposition,
    ExecutionLeaseRepository,
    ExecutionRegistration,
    ExecutionStateConflict,
    LeaseClaimDisposition,
    ProviderSession,
    WorkspaceWorktree,
    WorktreeRepository,
    WorktreeStateError,
)


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        return None


class _FakeSession:
    def __init__(self, scalar_results: Iterable[object | None]) -> None:
        self._scalar_results = iter(scalar_results)
        self.statements: list[object] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        return None

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        return object()

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return next(self._scalar_results)


class _FakeSessions:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


def _repository(*scalar_results: object | None) -> tuple[ExecutionLeaseRepository, _FakeSession]:
    session = _FakeSession(scalar_results)
    sessions = cast(async_sessionmaker[AsyncSession], _FakeSessions(session))
    return ExecutionLeaseRepository(sessions), session


def _worktree_repository(*scalar_results: object | None) -> WorktreeRepository:
    session = _FakeSession(scalar_results)
    sessions = cast(async_sessionmaker[AsyncSession], _FakeSessions(session))
    return WorktreeRepository(sessions)


def _registration() -> ExecutionRegistration:
    return ExecutionRegistration(
        execution_id="exec-001",
        idempotency_key="workflow-1:extract:1",
        workflow_instance_id="workflow-1",
        node_id="extract",
        activation=1,
        plan_hash="a" * 64,
        request_hash="b" * 64,
        output_schema_hash="c" * 64,
        provider="fake",
        model="fake-model",
        effort="high",
        workspace_id="repo",
        access_mode="workspace_write",
        session_mode="new",
    )


def _execution(
    *,
    status: str = "registered",
    owner: str | None = None,
    epoch: int = 0,
    expires_at: datetime | None = None,
    start_intent_at: datetime | None = None,
) -> AgentExecutionLease:
    registration = _registration()
    return AgentExecutionLease(
        execution_id=registration.execution_id,
        idempotency_key=registration.idempotency_key,
        workflow_instance_id=registration.workflow_instance_id,
        node_id=registration.node_id,
        activation=registration.activation,
        plan_hash=registration.plan_hash,
        request_hash=registration.request_hash,
        output_schema_hash=registration.output_schema_hash,
        provider=registration.provider,
        model=registration.model,
        effort=registration.effort,
        workspace_id=registration.workspace_id,
        access_mode=registration.access_mode,
        session_mode=registration.session_mode,
        status=status,
        lease_owner=owner,
        lease_epoch=epoch,
        lease_expires_at=expires_at,
        start_intent_at=start_intent_at,
        last_sequence=0,
    )


async def test_claim_acquires_and_fences_a_pre_start_execution() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    row = _execution()
    repository, _ = _repository(row, now)

    claim = await repository.claim(
        _registration(),
        lease_owner="worker-1/activity-1",
        lease_duration=timedelta(seconds=30),
    )

    assert claim.disposition is LeaseClaimDisposition.ACQUIRED
    assert claim.lease_epoch == 1
    assert row.status == "leased"
    assert row.lease_owner == "worker-1/activity-1"
    assert row.lease_expires_at == now + timedelta(seconds=30)


async def test_claim_never_takes_over_after_external_start_intent() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    row = _execution(
        status="running",
        owner="dead-worker/activity-1",
        epoch=3,
        expires_at=now - timedelta(seconds=1),
        start_intent_at=now - timedelta(minutes=1),
    )
    repository, _ = _repository(row, now)

    claim = await repository.claim(
        _registration(),
        lease_owner="worker-2/activity-2",
        lease_duration=timedelta(seconds=30),
    )

    assert claim.disposition is LeaseClaimDisposition.RECONCILIATION_REQUIRED
    assert row.status == "reconciliation_required"
    assert row.lease_owner is None
    assert row.reconcile_reason == "execution lease expired after external start intent"


async def test_expired_owner_cannot_revive_itself_after_start_intent() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    owner = "worker-1/activity-1"
    row = _execution(
        status="running",
        owner=owner,
        epoch=3,
        expires_at=now,
        start_intent_at=now - timedelta(minutes=1),
    )
    repository, _ = _repository(row, now)

    claim = await repository.claim(
        _registration(),
        lease_owner=owner,
        lease_duration=timedelta(seconds=30),
    )

    assert claim.disposition is LeaseClaimDisposition.RECONCILIATION_REQUIRED
    assert row.status == "reconciliation_required"


async def test_renew_rejects_an_expired_fence() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    row = _execution(
        status="running",
        owner="worker-1/activity-1",
        epoch=2,
        expires_at=now - timedelta(microseconds=1),
        start_intent_at=now - timedelta(minutes=1),
    )
    repository, _ = _repository(row, now)

    renewed = await repository.renew(
        row.execution_id,
        lease_owner="worker-1/activity-1",
        lease_epoch=2,
        lease_duration=timedelta(seconds=30),
    )

    assert renewed is False


async def test_provider_session_requires_durable_start_intent() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    owner = "worker-1/activity-1"
    row = _execution(
        status="leased",
        owner=owner,
        epoch=2,
        expires_at=now + timedelta(minutes=1),
    )
    repository, _ = _repository(row, now)

    with pytest.raises(ExecutionStateConflict, match="durable external start intent"):
        await repository.record_session(
            row.execution_id,
            lease_owner=owner,
            lease_epoch=2,
            provider="fake",
            native_session_id="native-session-1",
            workspace_id="repo",
            lease_duration=timedelta(seconds=30),
        )


async def test_session_start_checkpoint_and_finalize_preserve_the_fence() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    owner = "worker-1/activity-1"
    expires = now + timedelta(minutes=1)
    row = _execution(status="leased", owner=owner, epoch=2, expires_at=expires)
    provider_session = ProviderSession(
        session_key="d" * 64,
        provider="fake",
        native_session_id="native-session-1",
        workspace_id="repo",
        status="open",
        claim_epoch=0,
    )
    repository, _ = _repository(row, now)

    started_at = await repository.mark_start_intent(
        row.execution_id,
        lease_owner=owner,
        lease_epoch=2,
    )
    assert started_at == now
    assert row.status == "starting"

    repository, _ = _repository(row, now, provider_session)

    session_claim = await repository.record_session(
        row.execution_id,
        lease_owner=owner,
        lease_epoch=2,
        provider="fake",
        native_session_id="native-session-1",
        workspace_id="repo",
        lease_duration=timedelta(seconds=30),
    )

    assert session_claim.claim_epoch == 1
    assert row.status == "starting"
    assert provider_session.active_execution_id == row.execution_id

    repository, _ = _repository(row, now)
    await repository.checkpoint(
        row.execution_id,
        lease_owner=owner,
        lease_epoch=2,
        status="running",
        sequence=4,
        native_operation_id="turn-1",
        process_id=1234,
        process_started_at=now,
    )
    assert row.status == "running"
    assert row.last_sequence == 4
    assert row.process_id == 1234

    repository, _ = _repository(row, now, provider_session)
    await repository.finalize(
        row.execution_id,
        lease_owner=owner,
        lease_epoch=2,
        status="succeeded",
        result_payload={"answer": 3},
    )
    assert row.status == "succeeded"
    assert row.result_payload == {"answer": 3}
    assert row.lease_owner is None
    assert provider_session.active_execution_id is None

    repository, _ = _repository(row, now)
    await repository.finalize(
        row.execution_id,
        lease_owner=owner,
        lease_epoch=2,
        status="succeeded",
        result_payload={"answer": 3},
    )

    repository, _ = _repository(row, now)
    with pytest.raises(ExecutionStateConflict, match="different data"):
        await repository.finalize(
            row.execution_id,
            lease_owner=owner,
            lease_epoch=2,
            status="succeeded",
            result_payload={"answer": 4},
        )


async def test_worktree_cleanup_requires_a_fenced_claim() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    row = WorkspaceWorktree(
        worktree_id="worktree-1",
        execution_id="exec-001",
        workspace_id="repo",
        relative_path="repo/worktree-1",
        base_commit="a" * 40,
        state="in_use",
        cleanup_attempts=0,
        cleanup_epoch=0,
    )
    repository = _worktree_repository(row, now)

    claim = await repository.claim_cleanup(
        row.execution_id,
        cleanup_owner="worker-1/activity-1",
        lease_duration=timedelta(seconds=30),
    )

    assert claim.disposition is CleanupClaimDisposition.ACQUIRED
    assert claim.cleanup_epoch == 1
    assert row.state == "cleanup_pending"

    repository = _worktree_repository(row)
    await repository.finish_cleanup(
        row.execution_id,
        cleanup_owner="worker-1/activity-1",
        cleanup_epoch=claim.cleanup_epoch,
        disposition="preserved",
    )
    assert row.state == "preserved"

    repository = _worktree_repository(row)
    with pytest.raises(WorktreeStateError, match="cannot become cleaned"):
        await repository.finish_cleanup(
            row.execution_id,
            cleanup_owner="late-worker/activity-2",
            cleanup_epoch=claim.cleanup_epoch,
            disposition="removed",
        )


async def test_preserved_worktree_is_a_terminal_cleanup_state() -> None:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    row = WorkspaceWorktree(
        worktree_id="worktree-1",
        execution_id="exec-001",
        workspace_id="repo",
        relative_path="repo/worktree-1",
        base_commit="a" * 40,
        state="preserved",
        cleanup_attempts=1,
        cleanup_epoch=1,
    )
    repository = _worktree_repository(row, now)

    claim = await repository.claim_cleanup(
        row.execution_id,
        cleanup_owner="worker-2/activity-2",
        lease_duration=timedelta(seconds=30),
    )

    assert claim.disposition is CleanupClaimDisposition.TERMINAL
    assert claim.state == "preserved"


def test_phase3_models_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    tables: tuple[Table, ...] = (
        cast(Table, AgentExecutionLease.__table__),
        cast(Table, AgentExecutionAttempt.__table__),
        cast(Table, ProviderSession.__table__),
        cast(Table, WorkspaceWorktree.__table__),
    )

    sql = "\n".join(str(CreateTable(table).compile(dialect=dialect)) for table in tables)
    index_sql = "\n".join(
        str(CreateIndex(index).compile(dialect=dialect))
        for table in tables
        for index in table.indexes
    )

    assert "CREATE TABLE agent_execution_leases" in sql
    assert "result_payload JSONB" in sql
    assert "FOREIGN KEY(execution_id) REFERENCES agent_execution_leases" in sql
    assert "CREATE INDEX ix_agent_execution_status_lease" in index_sql
    assert "CREATE INDEX ix_workspace_worktree_cleanup" in index_sql


def test_phase3_migration_is_the_single_alembic_head() -> None:
    project_root = Path(__file__).parents[1]
    config = Config(project_root / "alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    revision = scripts.get_revision(CURRENT_SCHEMA_REVISION)

    assert scripts.get_heads() == [CURRENT_SCHEMA_REVISION]
    assert revision is not None
    assert revision.down_revision == "0001_phase1_baseline"


def test_phase3_migration_renders_complete_postgresql_offline_sql() -> None:
    project_root = Path(__file__).parents[1]
    output = StringIO()
    config = Config(project_root / "alembic.ini", output_buffer=output)

    command.upgrade(config, "head", sql=True)

    sql = output.getvalue()
    assert "CREATE TABLE agent_execution_leases" in sql
    assert "CREATE TABLE agent_execution_attempts" in sql
    assert "CREATE TABLE provider_sessions" in sql
    assert "CREATE TABLE workspace_worktrees" in sql
    assert f"'{CURRENT_SCHEMA_REVISION}'" in sql
