"""Add control-plane configuration, projections, event inbox, and command outbox.

Revision ID: 0003_phase4_control_plane
Revises: 0002_phase3_agent_execution
Create Date: 2026-08-16
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_phase4_control_plane"
down_revision: str | None = "0002_phase3_agent_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column[datetime], sa.Column[datetime]]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "workflow_templates",
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "latest_version >= 0",
            name="ck_workflow_template_latest_nonnegative",
        ),
        sa.CheckConstraint("revision > 0", name="ck_workflow_template_revision_positive"),
        sa.PrimaryKeyConstraint("template_id"),
    )
    op.create_table(
        "workflow_template_versions",
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "definition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "compiled_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("catalog_revision", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_workflow_template_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["workflow_templates.template_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("template_id", "version"),
        sa.UniqueConstraint(
            "template_id",
            "plan_hash",
            name="uq_workflow_template_version_plan",
        ),
    )
    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "char_length(request_hash) = 64",
            name="ck_idempotency_record_request_hash",
        ),
        sa.PrimaryKeyConstraint("scope", "idempotency_key"),
    )
    op.create_index(
        "ix_idempotency_record_created",
        "idempotency_records",
        ["created_at"],
    )
    op.create_table(
        "workflow_instance_projection",
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("temporal_workflow_id", sa.String(length=512), nullable=False),
        sa.Column("temporal_run_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "workflow_input",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "trigger_cause",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("projection_version", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "projection_version >= 0",
            name="ck_workflow_instance_projection_version_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending_start', 'running', 'waiting', 'succeeded', 'failed', "
            "'cancelled', 'attention_required')",
            name="ck_workflow_instance_projection_status",
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["workflow_template_versions.template_id", "workflow_template_versions.version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("instance_id"),
        sa.UniqueConstraint("temporal_workflow_id"),
    )
    op.create_index(
        "ix_workflow_instance_projection_status",
        "workflow_instance_projection",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_workflow_instance_projection_template",
        "workflow_instance_projection",
        ["template_id", "template_version"],
    )
    op.create_table(
        "workflow_node_projection",
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("activation", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("projection_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("activation >= 0", name="ck_workflow_node_projection_activation"),
        sa.CheckConstraint(
            "projection_version >= 0",
            name="ck_workflow_node_projection_version_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'waiting_approval', 'waiting_event', "
            "'succeeded', 'failed', 'timed_out', 'cancelled', 'skipped', "
            "'reconciliation_required')",
            name="ck_workflow_node_projection_status",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["workflow_instance_projection.instance_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instance_id", "node_id", "activation"),
    )
    op.create_index(
        "ix_workflow_node_projection_status",
        "workflow_node_projection",
        ["instance_id", "status"],
    )
    op.create_table(
        "execution_attempt_projection",
        sa.Column("attempt_projection_id", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("execution_id", sa.String(length=512), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("effort", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_session_id", sa.String(length=512), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt > 0",
            name="ck_execution_attempt_projection_attempt",
        ),
        sa.CheckConstraint(
            "status IN ('started', 'running', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'reconciliation_required')",
            name="ck_execution_attempt_projection_status",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["workflow_instance_projection.instance_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_projection_id"),
        sa.UniqueConstraint(
            "execution_id",
            "attempt",
            name="uq_execution_attempt_projection_execution_attempt",
        ),
    )
    op.create_index(
        "ix_execution_attempt_projection_instance",
        "execution_attempt_projection",
        ["instance_id", "updated_at"],
    )
    op.create_table(
        "approval_projection",
        sa.Column("approval_id", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("activation", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=True),
        sa.Column("operator_label", sa.String(length=256), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("activation > 0", name="ck_approval_projection_activation"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'timed_out', 'cancelled')",
            name="ck_approval_projection_status",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["workflow_instance_projection.instance_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint("command_id"),
        sa.UniqueConstraint(
            "instance_id",
            "node_id",
            "activation",
            name="uq_approval_projection_activation",
        ),
    )
    op.create_index(
        "ix_approval_projection_pending",
        "approval_projection",
        ["status", "requested_at"],
    )
    op.create_table(
        "event_inbox",
        sa.Column("inbox_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=512), nullable=False),
        sa.Column("event_id", sa.String(length=512), nullable=False),
        sa.Column("event_type", sa.String(length=256), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_content_type", sa.String(length=256), nullable=True),
        sa.Column("data_schema", sa.String(length=1024), nullable=True),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "extensions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('received', 'routed', 'ignored', 'failed')",
            name="ck_event_inbox_status",
        ),
        sa.PrimaryKeyConstraint("inbox_id"),
        sa.UniqueConstraint("source", "event_id", name="uq_event_inbox_source_id"),
    )
    op.create_index(
        "ix_event_inbox_status_received",
        "event_inbox",
        ["status", "received_at"],
    )
    op.create_index(
        "ix_event_inbox_type_subject",
        "event_inbox",
        ["event_type", "subject"],
    )
    op.create_table(
        "event_wait_subscriptions",
        sa.Column("subscription_id", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("temporal_workflow_id", sa.String(length=512), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("activation", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=256), nullable=False),
        sa.Column("source_pattern", sa.String(length=512), nullable=True),
        sa.Column("subject_pattern", sa.String(length=512), nullable=True),
        sa.Column("correlation_key", sa.String(length=512), nullable=True),
        sa.Column(
            "output_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("output_schema_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("delivery_command_id", sa.String(length=128), nullable=True),
        sa.Column("delivered_inbox_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "activation > 0",
            name="ck_event_wait_subscription_activation",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'delivered', 'closed', 'expired')",
            name="ck_event_wait_subscription_status",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["workflow_instance_projection.instance_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["delivered_inbox_id"],
            ["event_inbox.inbox_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("subscription_id"),
        sa.UniqueConstraint("delivery_command_id"),
        sa.UniqueConstraint(
            "instance_id",
            "node_id",
            "activation",
            name="uq_event_wait_subscription_activation",
        ),
    )
    op.create_index(
        "ix_event_wait_subscription_match",
        "event_wait_subscriptions",
        ["status", "event_type", "subject_pattern"],
    )
    op.create_index(
        "ix_event_wait_subscription_expiry",
        "event_wait_subscriptions",
        ["status", "expires_at"],
    )
    op.create_table(
        "workflow_events",
        sa.Column(
            "delivery_id",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=256), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["workflow_instance_projection.instance_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint("event_id", name="uq_workflow_event_id"),
    )
    op.create_index(
        "ix_workflow_events_instance_delivery",
        "workflow_events",
        ["instance_id", "delivery_id"],
    )
    op.create_table(
        "trigger_definitions",
        sa.Column("trigger_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("event_type", sa.String(length=256), nullable=False),
        sa.Column("source_pattern", sa.String(length=512), nullable=True),
        sa.Column("subject_pattern", sa.String(length=512), nullable=True),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column(
            "input_bindings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint("revision > 0", name="ck_trigger_definition_revision"),
        sa.CheckConstraint("template_version > 0", name="ck_trigger_template_version"),
        sa.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["workflow_template_versions.template_id", "workflow_template_versions.version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("trigger_id"),
    )
    op.create_index(
        "ix_trigger_definition_match",
        "trigger_definitions",
        ["enabled", "event_type"],
    )
    op.create_table(
        "trigger_deliveries",
        sa.Column("delivery_id", sa.String(length=64), nullable=False),
        sa.Column("trigger_id", sa.String(length=64), nullable=False),
        sa.Column("inbox_id", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('queued', 'started', 'ignored', 'failed')",
            name="ck_trigger_delivery_status",
        ),
        sa.ForeignKeyConstraint(
            ["inbox_id"],
            ["event_inbox.inbox_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["workflow_instance_projection.instance_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_id"],
            ["trigger_definitions.trigger_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint(
            "trigger_id",
            "inbox_id",
            name="uq_trigger_delivery_trigger_inbox",
        ),
    )
    op.create_index(
        "ix_trigger_delivery_instance",
        "trigger_deliveries",
        ["instance_id"],
    )
    op.create_table(
        "schedule_definitions",
        sa.Column("schedule_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("schedule_kind", sa.String(length=16), nullable=False),
        sa.Column(
            "schedule_spec",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "target",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "schedule_kind IN ('cron', 'interval', 'calendar')",
            name="ck_schedule_definition_kind",
        ),
        sa.CheckConstraint("revision > 0", name="ck_schedule_definition_revision"),
        sa.CheckConstraint(
            "target_kind IN ('workflow', 'git_connector')",
            name="ck_schedule_definition_target",
        ),
        sa.PrimaryKeyConstraint("schedule_id"),
    )
    op.create_index(
        "ix_schedule_definition_enabled",
        "schedule_definitions",
        ["enabled", "updated_at"],
    )
    op.create_table(
        "connector_checkpoints",
        sa.Column("connector_id", sa.String(length=64), nullable=False),
        sa.Column("connector_kind", sa.String(length=64), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_value", sa.String(length=2048), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_connector_checkpoint_revision",
        ),
        sa.PrimaryKeyConstraint("connector_id"),
    )
    op.create_index(
        "ix_connector_checkpoint_kind",
        "connector_checkpoints",
        ["connector_kind", "updated_at"],
    )
    op.create_table(
        "provider_catalog_snapshots",
        sa.Column("runtime_name", sa.String(length=64), nullable=False),
        sa.Column("runtime_id", sa.String(length=256), nullable=False),
        sa.Column("provider_id", sa.String(length=256), nullable=False),
        sa.Column("revision", sa.String(length=64), nullable=False),
        sa.Column(
            "models",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "char_length(revision) = 64",
            name="ck_provider_catalog_snapshot_revision",
        ),
        sa.PrimaryKeyConstraint("runtime_name"),
    )
    op.create_index(
        "ix_provider_catalog_snapshot_updated",
        "provider_catalog_snapshots",
        ["updated_at"],
    )
    op.create_table(
        "command_outbox",
        sa.Column("outbox_id", sa.String(length=64), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("command_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=512), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=256), nullable=True),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_command_outbox_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            "lease_epoch >= 0",
            name="ck_command_outbox_epoch_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatching', 'dispatched', 'failed', 'dead')",
            name="ck_command_outbox_status",
        ),
        sa.PrimaryKeyConstraint("outbox_id"),
        sa.UniqueConstraint("command_id"),
    )
    op.create_index(
        "ix_command_outbox_dispatch",
        "command_outbox",
        ["status", "available_at", "created_at"],
    )
    op.create_table(
        "audit_log",
        sa.Column(
            "audit_id",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("audit_event_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=512), nullable=False),
        sa.Column("operator_label", sa.String(length=256), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("audit_id"),
        sa.UniqueConstraint("audit_event_id", name="uq_audit_log_event_id"),
    )
    op.create_index(
        "ix_audit_log_target",
        "audit_log",
        ["target_type", "target_id", "audit_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_target", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_command_outbox_dispatch", table_name="command_outbox")
    op.drop_table("command_outbox")
    op.drop_index(
        "ix_provider_catalog_snapshot_updated",
        table_name="provider_catalog_snapshots",
    )
    op.drop_table("provider_catalog_snapshots")
    op.drop_index(
        "ix_connector_checkpoint_kind",
        table_name="connector_checkpoints",
    )
    op.drop_table("connector_checkpoints")
    op.drop_index("ix_schedule_definition_enabled", table_name="schedule_definitions")
    op.drop_table("schedule_definitions")
    op.drop_index("ix_trigger_delivery_instance", table_name="trigger_deliveries")
    op.drop_table("trigger_deliveries")
    op.drop_index("ix_trigger_definition_match", table_name="trigger_definitions")
    op.drop_table("trigger_definitions")
    op.drop_index("ix_workflow_events_instance_delivery", table_name="workflow_events")
    op.drop_table("workflow_events")
    op.drop_index(
        "ix_event_wait_subscription_expiry",
        table_name="event_wait_subscriptions",
    )
    op.drop_index(
        "ix_event_wait_subscription_match",
        table_name="event_wait_subscriptions",
    )
    op.drop_table("event_wait_subscriptions")
    op.drop_index("ix_event_inbox_type_subject", table_name="event_inbox")
    op.drop_index("ix_event_inbox_status_received", table_name="event_inbox")
    op.drop_table("event_inbox")
    op.drop_index("ix_approval_projection_pending", table_name="approval_projection")
    op.drop_table("approval_projection")
    op.drop_index(
        "ix_execution_attempt_projection_instance",
        table_name="execution_attempt_projection",
    )
    op.drop_table("execution_attempt_projection")
    op.drop_index("ix_workflow_node_projection_status", table_name="workflow_node_projection")
    op.drop_table("workflow_node_projection")
    op.drop_index(
        "ix_workflow_instance_projection_template",
        table_name="workflow_instance_projection",
    )
    op.drop_index(
        "ix_workflow_instance_projection_status",
        table_name="workflow_instance_projection",
    )
    op.drop_table("workflow_instance_projection")
    op.drop_index("ix_idempotency_record_created", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_table("workflow_template_versions")
    op.drop_table("workflow_templates")
