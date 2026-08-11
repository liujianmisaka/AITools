from __future__ import annotations

from datetime import datetime, timezone
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
    task_instance_id: str
    task_id: str
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
    task_instance_id: str | None = None
    execution_attempt_id: str | None = None
    provider: str | None = None
    kind: EventKind
    occurred_at: datetime
    summary: str
    payload: dict[str, Any]
    raw_event_type: str | None = None
