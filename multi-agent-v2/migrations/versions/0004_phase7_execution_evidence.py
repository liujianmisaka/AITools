"""Add durable Agent execution evidence and artifact metadata.

Revision ID: 0004_phase7_execution_evidence
Revises: 0003_phase4_control_plane
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_phase7_execution_evidence"
down_revision: str | None = "0003_phase4_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=512), nullable=True),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_artifact_sha256",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifact_size_nonnegative"),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["agent_execution_leases.execution_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.UniqueConstraint("relative_path"),
    )
    op.create_index(
        "ix_artifact_execution",
        "artifacts",
        ["execution_id", "created_at"],
    )
    op.create_index("ix_artifact_kind", "artifacts", ["kind", "created_at"])
    op.create_table(
        "agent_execution_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=512), nullable=False),
        sa.Column("attempt_id", sa.String(length=128), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_session_id", sa.String(length=512), nullable=True),
        sa.Column("provider_turn_id", sa.String(length=512), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name="ck_agent_execution_event_sequence_positive",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["agent_execution_attempts.attempt_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["agent_execution_leases.execution_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_agent_execution_event_sequence",
        ),
    )
    op.create_index(
        "ix_agent_execution_event_execution",
        "agent_execution_events",
        ["execution_id", "sequence"],
    )
    op.create_index(
        "ix_agent_execution_event_type",
        "agent_execution_events",
        ["event_type", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_execution_event_type", table_name="agent_execution_events")
    op.drop_index("ix_agent_execution_event_execution", table_name="agent_execution_events")
    op.drop_table("agent_execution_events")
    op.drop_index("ix_artifact_kind", table_name="artifacts")
    op.drop_index("ix_artifact_execution", table_name="artifacts")
    op.drop_table("artifacts")
