from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
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


class WorkflowTemplate(Base):
    __tablename__ = "workflow_templates"
    __table_args__ = (
        CheckConstraint("latest_version >= 0", name="ck_workflow_template_latest_nonnegative"),
        CheckConstraint("revision > 0", name="ck_workflow_template_revision_positive"),
    )

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkflowTemplateVersion(Base):
    __tablename__ = "workflow_template_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_workflow_template_version_positive"),
        UniqueConstraint(
            "template_id",
            "plan_hash",
            name="uq_workflow_template_version_plan",
        ),
    )

    template_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_templates.template_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    compiled_plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint(
            "char_length(request_hash) = 64",
            name="ck_idempotency_record_request_hash",
        ),
        Index("ix_idempotency_record_created", "created_at"),
    )

    scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkflowInstanceProjection(Base):
    __tablename__ = "workflow_instance_projection"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_start', 'running', 'waiting', 'succeeded', 'failed', "
            "'cancelled', 'attention_required')",
            name="ck_workflow_instance_projection_status",
        ),
        CheckConstraint(
            "projection_version >= 0",
            name="ck_workflow_instance_projection_version_nonnegative",
        ),
        Index("ix_workflow_instance_projection_status", "status", "updated_at"),
        Index("ix_workflow_instance_projection_template", "template_id", "template_version"),
    )

    instance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    temporal_workflow_id: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    temporal_run_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_start")
    workflow_input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    trigger_cause: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    projection_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        *__table_args__,
        ForeignKeyConstraint(
            ["template_id", "template_version"],
            [
                "workflow_template_versions.template_id",
                "workflow_template_versions.version",
            ],
            ondelete="RESTRICT",
        ),
    )


class WorkflowNodeProjection(Base):
    __tablename__ = "workflow_node_projection"
    __table_args__ = (
        CheckConstraint("activation >= 0", name="ck_workflow_node_projection_activation"),
        CheckConstraint(
            "status IN ('pending', 'running', 'waiting_approval', 'waiting_event', "
            "'succeeded', 'failed', 'timed_out', 'cancelled', 'skipped', "
            "'reconciliation_required')",
            name="ck_workflow_node_projection_status",
        ),
        CheckConstraint(
            "projection_version >= 0",
            name="ck_workflow_node_projection_version_nonnegative",
        ),
        Index("ix_workflow_node_projection_status", "instance_id", "status"),
    )

    instance_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_instance_projection.instance_id", ondelete="CASCADE"),
        primary_key=True,
    )
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    activation: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    projection_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionAttemptProjection(Base):
    __tablename__ = "execution_attempt_projection"
    __table_args__ = (
        CheckConstraint("attempt > 0", name="ck_execution_attempt_projection_attempt"),
        CheckConstraint(
            "status IN ('started', 'running', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'reconciliation_required')",
            name="ck_execution_attempt_projection_status",
        ),
        UniqueConstraint(
            "execution_id",
            "attempt",
            name="uq_execution_attempt_projection_execution_attempt",
        ),
        Index("ix_execution_attempt_projection_instance", "instance_id", "updated_at"),
    )

    attempt_projection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_instance_projection.instance_id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_id: Mapped[str] = mapped_column(String(512), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    effort: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_session_id: Mapped[str | None] = mapped_column(String(512))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApprovalProjection(Base):
    __tablename__ = "approval_projection"
    __table_args__ = (
        CheckConstraint("activation > 0", name="ck_approval_projection_activation"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'timed_out', 'cancelled')",
            name="ck_approval_projection_status",
        ),
        UniqueConstraint(
            "instance_id",
            "node_id",
            "activation",
            name="uq_approval_projection_activation",
        ),
        Index("ix_approval_projection_pending", "status", "requested_at"),
    )

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_instance_projection.instance_id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    activation: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    command_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    operator_label: Mapped[str | None] = mapped_column(String(256))
    reason: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventWaitSubscription(Base):
    __tablename__ = "event_wait_subscriptions"
    __table_args__ = (
        CheckConstraint("activation > 0", name="ck_event_wait_subscription_activation"),
        CheckConstraint(
            "status IN ('active', 'delivered', 'closed', 'expired')",
            name="ck_event_wait_subscription_status",
        ),
        UniqueConstraint(
            "instance_id",
            "node_id",
            "activation",
            name="uq_event_wait_subscription_activation",
        ),
        Index(
            "ix_event_wait_subscription_match",
            "status",
            "event_type",
            "subject_pattern",
        ),
        Index(
            "ix_event_wait_subscription_expiry",
            "status",
            "expires_at",
        ),
    )

    subscription_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_instance_projection.instance_id", ondelete="CASCADE"),
        nullable=False,
    )
    temporal_workflow_id: Mapped[str] = mapped_column(String(512), nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    activation: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(256), nullable=False)
    source_pattern: Mapped[str | None] = mapped_column(String(512))
    subject_pattern: Mapped[str | None] = mapped_column(String(512))
    correlation_key: Mapped[str | None] = mapped_column(String(512))
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    delivery_command_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    delivered_inbox_id: Mapped[str | None] = mapped_column(
        ForeignKey("event_inbox.inbox_id", ondelete="RESTRICT")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkflowEvent(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_workflow_event_id"),
        Index("ix_workflow_events_instance_delivery", "instance_id", "delivery_id"),
    )

    delivery_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_instance_projection.instance_id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(256), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EventInbox(Base):
    __tablename__ = "event_inbox"
    __table_args__ = (
        UniqueConstraint("source", "event_id", name="uq_event_inbox_source_id"),
        CheckConstraint(
            "status IN ('received', 'routed', 'ignored', 'failed')",
            name="ck_event_inbox_status",
        ),
        Index("ix_event_inbox_status_received", "status", "received_at"),
        Index("ix_event_inbox_type_subject", "event_type", "subject"),
    )

    inbox_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    event_id: Mapped[str] = mapped_column(String(512), nullable=False)
    event_type: Mapped[str] = mapped_column(String(256), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(512))
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_content_type: Mapped[str | None] = mapped_column(String(256))
    data_schema: Mapped[str | None] = mapped_column(String(1024))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    extensions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")
    error_message: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TriggerDefinition(Base):
    __tablename__ = "trigger_definitions"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_trigger_definition_revision"),
        CheckConstraint("template_version > 0", name="ck_trigger_template_version"),
        Index("ix_trigger_definition_match", "enabled", "event_type"),
        ForeignKeyConstraint(
            ["template_id", "template_version"],
            [
                "workflow_template_versions.template_id",
                "workflow_template_versions.version",
            ],
            ondelete="RESTRICT",
        ),
    )

    trigger_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    event_type: Mapped[str] = mapped_column(String(256), nullable=False)
    source_pattern: Mapped[str | None] = mapped_column(String(512))
    subject_pattern: Mapped[str | None] = mapped_column(String(512))
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_bindings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TriggerDelivery(Base):
    __tablename__ = "trigger_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'started', 'ignored', 'failed')",
            name="ck_trigger_delivery_status",
        ),
        UniqueConstraint(
            "trigger_id",
            "inbox_id",
            name="uq_trigger_delivery_trigger_inbox",
        ),
        Index("ix_trigger_delivery_instance", "instance_id"),
    )

    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trigger_id: Mapped[str] = mapped_column(
        ForeignKey("trigger_definitions.trigger_id", ondelete="RESTRICT"),
        nullable=False,
    )
    inbox_id: Mapped[str] = mapped_column(
        ForeignKey("event_inbox.inbox_id", ondelete="RESTRICT"),
        nullable=False,
    )
    instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_instance_projection.instance_id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScheduleDefinition(Base):
    __tablename__ = "schedule_definitions"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_schedule_definition_revision"),
        CheckConstraint(
            "schedule_kind IN ('cron', 'interval', 'calendar')",
            name="ck_schedule_definition_kind",
        ),
        CheckConstraint(
            "target_kind IN ('workflow', 'git_connector')",
            name="ck_schedule_definition_target",
        ),
        Index("ix_schedule_definition_enabled", "enabled", "updated_at"),
    )

    schedule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    schedule_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConnectorCheckpoint(Base):
    __tablename__ = "connector_checkpoints"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_connector_checkpoint_revision"),
        Index("ix_connector_checkpoint_kind", "connector_kind", "updated_at"),
    )

    connector_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connector_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_value: Mapped[str] = mapped_column(String(2048), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProviderCatalogSnapshot(Base):
    __tablename__ = "provider_catalog_snapshots"
    __table_args__ = (
        CheckConstraint(
            "char_length(revision) = 64",
            name="ck_provider_catalog_snapshot_revision",
        ),
        Index("ix_provider_catalog_snapshot_updated", "updated_at"),
    )

    runtime_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    runtime_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(256), nullable=False)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    models: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CommandOutbox(Base):
    __tablename__ = "command_outbox"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_command_outbox_attempts_nonnegative"),
        CheckConstraint("lease_epoch >= 0", name="ck_command_outbox_epoch_nonnegative"),
        CheckConstraint(
            "status IN ('pending', 'dispatching', 'dispatched', 'failed', 'dead')",
            name="ck_command_outbox_status",
        ),
        Index("ix_command_outbox_dispatch", "status", "available_at", "created_at"),
    )

    outbox_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    command_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(256))
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        UniqueConstraint("audit_event_id", name="uq_audit_log_event_id"),
        Index("ix_audit_log_target", "target_type", "target_id", "audit_id"),
    )

    audit_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    audit_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(512), nullable=False)
    operator_label: Mapped[str | None] = mapped_column(String(256))
    reason: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
