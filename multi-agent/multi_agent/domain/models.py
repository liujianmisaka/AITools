from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AccessMode(str, Enum):
    read_only = "read_only"
    workspace_write = "workspace_write"


class SessionMode(str, Enum):
    new = "new"
    resume = "resume"


class FailurePolicy(str, Enum):
    fail_fast = "fail_fast"
    continue_independent = "continue_independent"


class OrchestrationKind(str, Enum):
    dag = "dag"


class WorkflowInstanceStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"


class TaskInstanceStatus(str, Enum):
    pending = "pending"
    ready = "ready"
    running = "running"
    awaiting_approval = "awaiting_approval"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"
    blocked = "blocked"


TERMINAL_TASK_INSTANCE_STATUSES = {
    TaskInstanceStatus.succeeded,
    TaskInstanceStatus.failed,
    TaskInstanceStatus.cancelled,
    TaskInstanceStatus.interrupted,
    TaskInstanceStatus.blocked,
}


class EventKind(str, Enum):
    started = "started"
    message_delta = "message_delta"
    message_completed = "message_completed"
    tool_started = "tool_started"
    tool_completed = "tool_completed"
    approval_required = "approval_required"
    usage = "usage"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class TriggerConcurrencyPolicy(str, Enum):
    allow_parallel = "allow_parallel"
    skip_if_running = "skip_if_running"


class TriggerDeliveryStatus(str, Enum):
    pending = "pending"
    delivered = "delivered"
    skipped = "skipped"
    failed = "failed"


class TriggerEventStatus(str, Enum):
    received = "received"
    processed = "processed"
    failed = "failed"


class ScheduledTaskRunStatus(str, Enum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    interrupted = "interrupted"


class GitCommitSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(pattern=IDENTIFIER_PATTERN)
    remote: str = Field(default="origin", pattern=IDENTIFIER_PATTERN)
    branch: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_./-]+$",
    )
    fetch: bool = True

    @model_validator(mode="after")
    def validate_branch_name(self) -> "GitCommitSourceConfig":
        branch = self.branch
        if (
            branch.startswith((".", "-", "/"))
            or branch.endswith((".", "/"))
            or ".." in branch
            or "//" in branch
            or "/." in branch
            or branch.endswith(".lock")
            or ".lock/" in branch
        ):
            raise ValueError("branch is not a safe Git branch name")
        return self


@dataclass(frozen=True, slots=True)
class WorkItemSeed:
    logical_key: str
    executor_kind: str
    spec: dict[str, Any]
    activation_number: int = 1


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=10)
    idempotent: bool = False


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=IDENTIFIER_PATTERN)
    depends_on: list[str] = Field(default_factory=list)
    provider: str = Field(pattern=IDENTIFIER_PATTERN)
    role: str = Field(default="worker", min_length=1, max_length=100)
    prompt_template: str = Field(min_length=1)
    workspace_id: str = Field(pattern=IDENTIFIER_PATTERN)
    access: AccessMode = AccessMode.read_only
    session_mode: SessionMode = SessionMode.new
    provider_session_id: str | None = None
    output_schema: dict[str, Any] | None = None
    timeout_seconds: float = Field(default=300.0, gt=0, le=86_400)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    provider_options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_session(self) -> "TaskSpec":
        if self.session_mode == SessionMode.resume and not self.provider_session_id:
            raise ValueError("provider_session_id is required when session_mode is resume")
        if self.session_mode == SessionMode.new and self.provider_session_id is not None:
            raise ValueError("provider_session_id is only valid when session_mode is resume")
        if self.id in self.depends_on:
            raise ValueError("a task cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("depends_on contains duplicate task IDs")
        if self.access == AccessMode.workspace_write:
            if self.retry_policy.max_attempts > 1 and not self.retry_policy.idempotent:
                raise ValueError("write retries require retry_policy.idempotent=true")
        return self


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex, pattern=IDENTIFIER_PATTERN)
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=200)
    tasks: list[TaskSpec] = Field(min_length=1)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    failure_policy: FailurePolicy = FailurePolicy.continue_independent
    @model_validator(mode="after")
    def validate_dag(self) -> "WorkflowDefinition":
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique")

        known = set(task_ids)
        for task in self.tasks:
            missing = set(task.depends_on) - known
            if missing:
                raise ValueError(
                    f"task {task.id!r} references unknown dependencies: {sorted(missing)}"
                )

        graph = {task.id: task.depends_on for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("workflow dependencies contain a cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in graph[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


class TriggerBindingDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex, pattern=IDENTIFIER_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    source_type: str = Field(pattern=IDENTIFIER_PATTERN)
    event_type: str = Field(pattern=IDENTIFIER_PATTERN)
    event_version: int = Field(default=1, ge=1)
    source_key: str | None = Field(default=None, min_length=1, max_length=500)
    template_id: str = Field(pattern=IDENTIFIER_PATTERN)
    enabled: bool = True
    source_config: dict[str, Any] = Field(default_factory=dict)
    event_filter: dict[str, Any] = Field(default_factory=dict)
    input_mapping: dict[str, str] = Field(default_factory=dict)
    concurrency_policy: TriggerConcurrencyPolicy = (
        TriggerConcurrencyPolicy.allow_parallel
    )

    @model_validator(mode="after")
    def validate_mapping_paths(self) -> "TriggerBindingDefinition":
        if any(not path.strip() for path in self.event_filter):
            raise ValueError("event_filter paths cannot be empty")
        if any(not key.strip() for key in self.input_mapping):
            raise ValueError("input_mapping output keys cannot be empty")
        if any(not path.strip() for path in self.input_mapping.values()):
            raise ValueError("input_mapping source paths cannot be empty")
        return self


class TriggerEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: str = Field(pattern=IDENTIFIER_PATTERN)
    event_type: str = Field(pattern=IDENTIFIER_PATTERN)
    event_version: int = Field(default=1, ge=1)
    source_key: str | None = Field(default=None, min_length=1, max_length=500)
    dedup_key: str = Field(min_length=1, max_length=500)
    payload: dict[str, Any] = Field(default_factory=dict)


class WebhookSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_key: str = Field(pattern=IDENTIFIER_PATTERN)
    secret_ref: str | None = Field(default=None, min_length=1, max_length=200)
    signature_header: str = Field(
        default="x-hub-signature-256", min_length=1, max_length=100
    )
    signature_algorithm: str = Field(
        default="sha256", pattern=r"^(sha256|sha384|sha512)$"
    )
    require_signature: bool = True
    allowed_ip_cidrs: list[str] = Field(default_factory=list)
    max_payload_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)
    dedup_header: str | None = Field(
        default="x-event-key", min_length=1, max_length=100
    )
    dedup_window_seconds: int = Field(
        default=3600, ge=0, le=31_536_000
    )


class ScheduleTickPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    schedule_type: str
    scheduled_fire_time: datetime
    sequence: int = Field(ge=1)


class WorkflowInstanceCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_instance_id: str
    template_id: str | None = None
    template_version: int | None = None
    source: str
    kind: str
    cause_type: str
    status: str
    revision: int = 0
    trigger_binding_id: str | None = None
    trigger_event_id: str | None = None


class WorkflowInstanceStatusChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_instance_id: str
    old_status: str
    new_status: str
    revision: int
    error: str | None = None


class ApprovalUpdatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    workflow_instance_id: str
    work_item_id: str
    status: str
    decided_by: str | None = None
    reason: str | None = None


class ScheduleRunUpdatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_task_id: str
    run_id: str
    status: str
    scheduled_for: str | None = None
    error: str | None = None


class TriggerDeliveryFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_event_id: str
    trigger_binding_id: str
    delivery_id: str | None = None
    error: str


class CronScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    misfire_grace_seconds: int = Field(default=60, ge=1, le=86_400)
    coalesce: bool = True

    @model_validator(mode="after")
    def validate_expression_shape(self) -> "CronScheduleConfig":
        if len(self.expression.split()) != 5:
            raise ValueError("cron expression must contain exactly five fields")
        return self


class IntervalScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weeks: int = Field(default=0, ge=0, le=10_000)
    days: int = Field(default=0, ge=0, le=10_000)
    hours: int = Field(default=0, ge=0, le=10_000)
    minutes: int = Field(default=0, ge=0, le=10_000)
    seconds: int = Field(default=0, ge=0, le=10_000)
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    misfire_grace_seconds: int = Field(default=60, ge=1, le=86_400)
    coalesce: bool = True

    @model_validator(mode="after")
    def validate_interval(self) -> "IntervalScheduleConfig":
        if not any(
            (self.weeks, self.days, self.hours, self.minutes, self.seconds)
        ):
            raise ValueError(
                "interval schedule must have at least one non-zero time field"
            )
        if self.start_at is not None and self.end_at is not None:
            try:
                if self.start_at >= self.end_at:
                    raise ValueError("interval start_at must be before end_at")
            except TypeError as exc:
                raise ValueError(
                    "interval start_at and end_at must use the same timezone "
                    "awareness"
                ) from exc
        return self


class OneTimeScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_at: datetime
    misfire_grace_seconds: int = Field(default=60, ge=1, le=86_400)


class PollTriggerBindingActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(pattern=IDENTIFIER_PATTERN)


class PublishTriggerEventActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduledTaskDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex, pattern=IDENTIFIER_PATTERN)
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=200)
    schedule_type: str = Field(default="cron", pattern=IDENTIFIER_PATTERN)
    schedule: dict[str, Any]
    action_type: str = Field(
        default="poll_trigger_binding",
        pattern=IDENTIFIER_PATTERN,
    )
    action: dict[str, Any]
    enabled: bool = True


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resume_session: bool = False
    stream_events: bool = True
    steer_running_turn: bool = False
    cancel_running_turn: bool = False
    structured_output: bool = False
    approval_callback: bool = False
    read_only_mode: bool = True
    workspace_write_mode: bool = False


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    workflow_instance_id: str
    work_item_id: str
    logical_key: str
    prompt: str
    role: str
    workspace: Path
    access: AccessMode
    output_schema: dict[str, Any] | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class ProviderSessionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    session_id: str


class ProviderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EventKind
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    raw_event_type: str | None = None


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int
    workflow_instance_id: str
    work_item_id: str | None = None
    execution_attempt_id: str | None = None
    provider: str | None = None
    kind: EventKind
    occurred_at: datetime
    summary: str
    payload: dict[str, Any]
    raw_event_type: str | None = None
