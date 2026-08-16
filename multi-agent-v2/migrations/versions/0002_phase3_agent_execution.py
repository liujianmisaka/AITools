"""Add fenced Agent execution, attempt, session, and worktree state.

Revision ID: 0002_phase3_agent_execution
Revises: 0001_phase1_baseline
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_phase3_agent_execution"
down_revision: str | None = "0001_phase1_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_execution_leases",
        sa.Column("execution_id", sa.String(length=512), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("workflow_instance_id", sa.String(length=512), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("activation", sa.Integer(), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("output_schema_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("effort", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("access_mode", sa.String(length=32), nullable=False),
        sa.Column("session_mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_owner", sa.String(length=256), nullable=True),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("start_intent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_session_key", sa.String(length=64), nullable=True),
        sa.Column("provider_session_id", sa.String(length=512), nullable=True),
        sa.Column("native_operation_id", sa.String(length=512), nullable=True),
        sa.Column("process_id", sa.Integer(), nullable=True),
        sa.Column("process_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "result_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("result_artifact_ref", sa.String(length=512), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("reconcile_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "access_mode IN ('read_only', 'workspace_write')",
            name="ck_agent_execution_access_mode",
        ),
        sa.CheckConstraint(
            "activation > 0",
            name="ck_agent_execution_activation_positive",
        ),
        sa.CheckConstraint(
            "lease_epoch >= 0",
            name="ck_agent_execution_lease_epoch_nonnegative",
        ),
        sa.CheckConstraint(
            "last_sequence >= 0",
            name="ck_agent_execution_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            "session_mode IN ('new', 'resume')",
            name="ck_agent_execution_session_mode",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'registered', 'leased', 'session_prepared', 'starting', 'running', "
            "'finalizing', 'cancelling', 'succeeded', 'failed', 'timed_out', 'cancelled', "
            "'reconciliation_required')",
            name="ck_agent_execution_status",
        ),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint(
            "workflow_instance_id",
            "node_id",
            "activation",
            name="uq_agent_execution_logical_activation",
        ),
    )
    op.create_index(
        "ix_agent_execution_status_lease",
        "agent_execution_leases",
        ["status", "lease_expires_at"],
        unique=False,
    )

    op.create_table(
        "agent_execution_attempts",
        sa.Column("attempt_id", sa.String(length=128), nullable=False),
        sa.Column("execution_id", sa.String(length=512), nullable=False),
        sa.Column("temporal_workflow_run_id", sa.String(length=128), nullable=False),
        sa.Column("temporal_activity_id", sa.String(length=256), nullable=False),
        sa.Column("temporal_activity_run_id", sa.String(length=512), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=256), nullable=False),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_agent_execution_attempt_positive",
        ),
        sa.CheckConstraint(
            "lease_epoch > 0",
            name="ck_agent_attempt_lease_epoch_positive",
        ),
        sa.CheckConstraint(
            "last_sequence >= 0",
            name="ck_agent_attempt_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('started', 'running', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'reconciliation_required')",
            name="ck_agent_execution_attempt_status",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["agent_execution_leases.execution_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "execution_id",
            "attempt_number",
            name="uq_agent_execution_attempt_number",
        ),
        sa.UniqueConstraint(
            "temporal_activity_run_id",
            name="uq_agent_execution_temporal_activity_run",
        ),
    )
    op.create_index(
        "ix_agent_execution_attempt_execution",
        "agent_execution_attempts",
        ["execution_id", "attempt_number"],
        unique=False,
    )

    op.create_table(
        "provider_sessions",
        sa.Column("session_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("native_session_id", sa.String(length=512), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("active_execution_id", sa.String(length=512), nullable=True),
        sa.Column("claim_owner", sa.String(length=256), nullable=True),
        sa.Column("claim_epoch", sa.BigInteger(), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "claim_epoch >= 0",
            name="ck_provider_session_claim_epoch_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'unknown')",
            name="ck_provider_session_status",
        ),
        sa.ForeignKeyConstraint(
            ["active_execution_id"],
            ["agent_execution_leases.execution_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_key"),
        sa.UniqueConstraint(
            "provider",
            "native_session_id",
            name="uq_provider_native_session",
        ),
    )
    op.create_index(
        "ix_provider_session_active_execution",
        "provider_sessions",
        ["active_execution_id"],
        unique=False,
    )

    op.create_table(
        "workspace_worktrees",
        sa.Column("worktree_id", sa.String(length=128), nullable=False),
        sa.Column("execution_id", sa.String(length=512), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("base_commit", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("result_commit", sa.String(length=64), nullable=True),
        sa.Column("patch_artifact_ref", sa.String(length=512), nullable=True),
        sa.Column("cleanup_attempts", sa.Integer(), nullable=False),
        sa.Column("cleanup_owner", sa.String(length=256), nullable=True),
        sa.Column("cleanup_epoch", sa.BigInteger(), nullable=False),
        sa.Column("cleanup_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "cleanup_attempts >= 0",
            name="ck_worktree_cleanup_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            "cleanup_epoch >= 0",
            name="ck_worktree_cleanup_epoch_nonnegative",
        ),
        sa.CheckConstraint(
            "state IN ('preparing', 'ready', 'in_use', 'cleanup_pending', 'cleaned', "
            "'cleanup_failed', 'preserved')",
            name="ck_workspace_worktree_state",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["agent_execution_leases.execution_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("worktree_id"),
        sa.UniqueConstraint("execution_id"),
        sa.UniqueConstraint("relative_path"),
    )
    op.create_index(
        "ix_workspace_worktree_cleanup",
        "workspace_worktrees",
        ["state", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_worktree_cleanup", table_name="workspace_worktrees")
    op.drop_table("workspace_worktrees")
    op.drop_index("ix_provider_session_active_execution", table_name="provider_sessions")
    op.drop_table("provider_sessions")
    op.drop_index("ix_agent_execution_attempt_execution", table_name="agent_execution_attempts")
    op.drop_table("agent_execution_attempts")
    op.drop_index("ix_agent_execution_status_lease", table_name="agent_execution_leases")
    op.drop_table("agent_execution_leases")
