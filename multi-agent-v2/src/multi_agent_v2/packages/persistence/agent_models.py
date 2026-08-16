from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from multi_agent_v2.packages.persistence.model_base import Base


class AgentExecutionLease(Base):
    __tablename__ = "agent_execution_leases"
    __table_args__ = (
        UniqueConstraint(
            "workflow_instance_id",
            "node_id",
            "activation",
            name="uq_agent_execution_logical_activation",
        ),
        CheckConstraint("activation > 0", name="ck_agent_execution_activation_positive"),
        CheckConstraint("lease_epoch >= 0", name="ck_agent_execution_lease_epoch_nonnegative"),
        CheckConstraint("last_sequence >= 0", name="ck_agent_execution_sequence_nonnegative"),
        CheckConstraint(
            "access_mode IN ('read_only', 'workspace_write')",
            name="ck_agent_execution_access_mode",
        ),
        CheckConstraint(
            "session_mode IN ('new', 'resume')",
            name="ck_agent_execution_session_mode",
        ),
        CheckConstraint(
            "status IN ("
            "'registered', 'leased', 'session_prepared', 'starting', 'running', "
            "'finalizing', 'cancelling', 'succeeded', 'failed', 'timed_out', 'cancelled', "
            "'reconciliation_required')",
            name="ck_agent_execution_status",
        ),
        Index("ix_agent_execution_status_lease", "status", "lease_expires_at"),
    )

    execution_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    workflow_instance_id: Mapped[str] = mapped_column(String(512), nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    activation: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    effort: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    access_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    session_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="registered")

    lease_owner: Mapped[str | None] = mapped_column(String(256))
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    provider_session_key: Mapped[str | None] = mapped_column(String(64))
    provider_session_id: Mapped[str | None] = mapped_column(String(512))
    native_operation_id: Mapped[str | None] = mapped_column(String(512))
    process_id: Mapped[int | None] = mapped_column(Integer)
    process_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_artifact_ref: Mapped[str | None] = mapped_column(String(512))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    reconcile_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentExecutionAttempt(Base):
    __tablename__ = "agent_execution_attempts"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "attempt_number",
            name="uq_agent_execution_attempt_number",
        ),
        UniqueConstraint(
            "temporal_activity_run_id",
            name="uq_agent_execution_temporal_activity_run",
        ),
        CheckConstraint("attempt_number > 0", name="ck_agent_execution_attempt_positive"),
        CheckConstraint("lease_epoch > 0", name="ck_agent_attempt_lease_epoch_positive"),
        CheckConstraint("last_sequence >= 0", name="ck_agent_attempt_sequence_nonnegative"),
        CheckConstraint(
            "status IN ('started', 'running', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'reconciliation_required')",
            name="ck_agent_execution_attempt_status",
        ),
        Index("ix_agent_execution_attempt_execution", "execution_id", "attempt_number"),
    )

    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("agent_execution_leases.execution_id", ondelete="RESTRICT"), nullable=False
    )
    temporal_workflow_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    temporal_activity_id: Mapped[str] = mapped_column(String(256), nullable=False)
    temporal_activity_run_id: Mapped[str | None] = mapped_column(String(512))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(256), nullable=False)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False, default="started")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderSession(Base):
    __tablename__ = "provider_sessions"
    __table_args__ = (
        UniqueConstraint("provider", "native_session_id", name="uq_provider_native_session"),
        CheckConstraint("claim_epoch >= 0", name="ck_provider_session_claim_epoch_nonnegative"),
        CheckConstraint(
            "status IN ('open', 'closed', 'unknown')",
            name="ck_provider_session_status",
        ),
        Index("ix_provider_session_active_execution", "active_execution_id"),
    )

    session_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    native_session_id: Mapped[str] = mapped_column(String(512), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    active_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_execution_leases.execution_id", ondelete="RESTRICT")
    )
    claim_owner: Mapped[str | None] = mapped_column(String(256))
    claim_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceWorktree(Base):
    __tablename__ = "workspace_worktrees"
    __table_args__ = (
        CheckConstraint("cleanup_attempts >= 0", name="ck_worktree_cleanup_attempts_nonnegative"),
        CheckConstraint("cleanup_epoch >= 0", name="ck_worktree_cleanup_epoch_nonnegative"),
        CheckConstraint(
            "state IN ('preparing', 'ready', 'in_use', 'cleanup_pending', 'cleaned', "
            "'cleanup_failed', 'preserved')",
            name="ck_workspace_worktree_state",
        ),
        Index("ix_workspace_worktree_cleanup", "state", "updated_at"),
    )

    worktree_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("agent_execution_leases.execution_id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    base_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="preparing")
    result_commit: Mapped[str | None] = mapped_column(String(64))
    patch_artifact_ref: Mapped[str | None] = mapped_column(String(512))
    cleanup_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cleanup_owner: Mapped[str | None] = mapped_column(String(256))
    cleanup_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cleanup_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
