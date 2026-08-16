from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.domain.models import JsonModel
from multi_agent_v2.packages.workflow_dsl import ExecutablePlan
from multi_agent_v2.packages.workflow_dsl.ir import StrictSchemaIr


class ControlModel(JsonModel):
    pass


class TemplateCreate(ControlModel):
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=16_384)


class TemplateRecord(ControlModel):
    template_id: str
    name: str
    description: str | None
    latest_version: int = Field(ge=0)
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class TemplateVersionCreate(ControlModel):
    definition: JsonObject


class TemplateVersionRecord(ControlModel):
    template_id: str
    version: int = Field(ge=1)
    definition: JsonObject
    compiled_plan: ExecutablePlan
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_revision: str
    created_at: datetime


class CatalogModelRecord(ControlModel):
    id: str
    label: str
    model_type: str
    efforts: tuple[str, ...]
    recommended_effort: str | None = None


class ProviderCatalogRecord(ControlModel):
    runtime_name: str
    runtime_id: str
    provider_id: str
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    models: tuple[CatalogModelRecord, ...]
    updated_at: datetime


class InstanceStart(ControlModel):
    template_version: int | None = Field(default=None, ge=1)
    workflow_input: JsonObject = Field(default_factory=dict)
    trigger_cause: JsonObject | None = None


class InstanceRecord(ControlModel):
    instance_id: str
    template_id: str
    template_version: int = Field(ge=1)
    temporal_workflow_id: str
    temporal_run_id: str | None = None
    status: Literal[
        "pending_start",
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
        "attention_required",
    ]
    workflow_input: JsonObject
    output: JsonObject | None = None
    error_code: str | None = None
    error_message: str | None = None
    trigger_cause: JsonObject | None = None
    projection_version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class NodeProjectionRecord(ControlModel):
    instance_id: str
    node_id: str
    activation: int = Field(ge=0)
    execution_id: str | None
    status: str
    output: JsonObject | None
    error_code: str | None
    error_message: str | None
    projection_version: int = Field(ge=0)
    updated_at: datetime


class ApprovalRecord(ControlModel):
    approval_id: str
    instance_id: str
    node_id: str
    activation: int = Field(ge=1)
    label: str
    status: Literal["pending", "approved", "rejected", "timed_out", "cancelled"]
    command_id: str | None
    operator_label: str | None
    reason: str | None
    requested_at: datetime
    decided_at: datetime | None
    expires_at: datetime | None


class WorkflowEventRecord(ControlModel):
    delivery_id: int = Field(ge=1)
    event_id: str
    instance_id: str | None
    event_type: str
    data: JsonObject
    occurred_at: datetime
    created_at: datetime


class InstanceDetail(ControlModel):
    instance: InstanceRecord
    nodes: tuple[NodeProjectionRecord, ...]
    approvals: tuple[ApprovalRecord, ...]


class TriggerCreate(ControlModel):
    trigger_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=256)
    enabled: bool = True
    event_type: str = Field(min_length=1, max_length=256)
    source_pattern: str | None = Field(default=None, max_length=512)
    subject_pattern: str | None = Field(default=None, max_length=512)
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    template_version: int = Field(ge=1)
    input_bindings: dict[str, str] = Field(default_factory=dict)

    @field_validator("input_bindings")
    @classmethod
    def bindings_must_be_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 128:
            raise ValueError("trigger input bindings exceed the maximum count")
        for name, expression in value.items():
            if not name or len(name) > 64 or not expression or len(expression) > 4096:
                raise ValueError("trigger input bindings must have bounded names and expressions")
        return value


class TriggerUpdate(ControlModel):
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=256)
    enabled: bool
    event_type: str = Field(min_length=1, max_length=256)
    source_pattern: str | None = Field(default=None, max_length=512)
    subject_pattern: str | None = Field(default=None, max_length=512)
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    template_version: int = Field(ge=1)
    input_bindings: dict[str, str] = Field(default_factory=dict)


class TriggerRecord(ControlModel):
    trigger_id: str
    name: str
    revision: int = Field(ge=1)
    enabled: bool
    event_type: str
    source_pattern: str | None
    subject_pattern: str | None
    template_id: str
    template_version: int
    input_bindings: dict[str, str]
    created_at: datetime
    updated_at: datetime


class ScheduleCreate(ControlModel):
    schedule_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=256)
    enabled: bool = True
    schedule_kind: Literal["cron", "interval", "calendar"]
    schedule_spec: JsonObject
    target_kind: Literal["workflow", "git_connector"]
    target: JsonObject


class ScheduleUpdate(ControlModel):
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=256)
    enabled: bool
    schedule_kind: Literal["cron", "interval", "calendar"]
    schedule_spec: JsonObject
    target_kind: Literal["workflow", "git_connector"]
    target: JsonObject


class ScheduleRecord(ControlModel):
    schedule_id: str
    name: str
    revision: int = Field(ge=1)
    enabled: bool
    schedule_kind: Literal["cron", "interval", "calendar"]
    schedule_spec: JsonObject
    target_kind: Literal["workflow", "git_connector"]
    target: JsonObject
    created_at: datetime
    updated_at: datetime


class ScheduleTriggerInput(ControlModel):
    schedule_id: str
    schedule_revision: int = Field(ge=1)
    target: JsonObject


class ScheduleFireRequest(ControlModel):
    schedule_id: str
    schedule_revision: int = Field(ge=1)
    occurrence_id: str = Field(min_length=1, max_length=256)
    target: JsonObject


class ProjectionEvent(ControlModel):
    event_id: str = Field(min_length=1, max_length=64)
    instance_id: str | None = Field(default=None, max_length=64)
    event_type: str = Field(min_length=1, max_length=256)
    data: JsonObject
    occurred_at: datetime


class ProjectionError(ControlModel):
    code: str
    message: str


class ProjectionNodeState(ControlModel):
    node_id: str = Field(min_length=1, max_length=64)
    status: Literal[
        "pending",
        "running",
        "waiting_approval",
        "waiting_event",
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "skipped",
        "reconciliation_required",
    ]
    activation: int = Field(ge=0)
    execution_id: str | None = Field(default=None, max_length=512)
    output: JsonObject | None = None
    error: ProjectionError | None = None
    approval_label: str | None = Field(default=None, max_length=512)


class WorkflowSnapshotProjection(ControlModel):
    schema_version: Literal[1] = 1
    temporal_workflow_id: str = Field(min_length=1, max_length=512)
    temporal_run_id: str | None = Field(default=None, max_length=128)
    status: Literal["running", "succeeded", "failed", "cancelled", "attention_required"]
    projection_version: int = Field(ge=0)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    nodes: tuple[ProjectionNodeState, ...]
    output: JsonObject | None = None
    error: ProjectionError | None = None


class EventWaitRegistration(ControlModel):
    subscription_id: str = Field(min_length=1, max_length=64)
    instance_id: str = Field(min_length=1, max_length=64)
    temporal_workflow_id: str = Field(min_length=1, max_length=512)
    node_id: str = Field(min_length=1, max_length=64)
    activation: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=256)
    source_pattern: str | None = Field(default=None, max_length=512)
    subject_pattern: str | None = Field(default=None, max_length=512)
    correlation_key: str | None = Field(default=None, max_length=512)
    output_schema: StrictSchemaIr
    expires_at: datetime | None = None


class ApprovalDecision(ControlModel):
    decision: Literal["approved", "rejected"]
    operator_label: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=16_384)


class CommandAccepted(ControlModel):
    command_id: str
    accepted: bool


class WorkflowSignal(ControlModel):
    signal_name: str = Field(min_length=1, max_length=256)
    data: JsonObject


class WorkflowUpdate(ControlModel):
    data: JsonObject


class OutboxCommand(ControlModel):
    outbox_id: str
    command_id: str
    command_type: str
    aggregate_type: str
    aggregate_id: str
    payload: JsonObject
    attempts: int = Field(ge=1)
    lease_owner: str
    lease_epoch: int = Field(ge=1)


class OutboxDispatchResult(ControlModel):
    command_id: str
    status: Literal["dispatched", "retrying", "dead"]
    error: str | None = None


class GitRefTarget(ControlModel):
    connector_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    workspace_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    remote: str = Field(default="origin", min_length=1, max_length=256)
    branch: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def remote_and_branch_must_not_be_options(self) -> GitRefTarget:
        if self.remote.startswith("-") or self.branch.startswith("-"):
            raise ValueError("Git remote and branch must not be command options")
        return self
