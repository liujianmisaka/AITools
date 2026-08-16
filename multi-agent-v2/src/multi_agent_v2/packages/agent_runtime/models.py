from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_dsl.ir import StrictSchemaIr

NonBlank = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=512,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]
EffortName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]
PromptText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=262_144),
]


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _non_blank(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must not be blank")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("value must not contain control characters")
    return normalized


class AgentRuntimeCapabilities(AgentModel):
    new_session: bool = False
    resume_session: bool = False
    stream_events: bool = False
    steer_running_turn: bool = False
    cancel_running_turn: bool = False
    structured_output: bool = False
    reconcile_execution: bool = False
    read_only_mode: bool = False
    workspace_write_mode: bool = False


class AgentModelSpec(AgentModel):
    id: NonBlank
    label: NonBlank
    model_type: NonBlank
    efforts: tuple[EffortName, ...]
    recommended_effort: EffortName | None = None

    @field_validator("id", "label", "model_type")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        return _non_blank(value)

    @field_validator("efforts")
    @classmethod
    def efforts_must_be_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_non_blank(value) for value in values)
        if not normalized:
            raise ValueError("at least one explicit effort is required")
        if len(normalized) != len(set(normalized)):
            raise ValueError("efforts must be unique")
        return normalized

    @model_validator(mode="after")
    def recommended_effort_must_be_supported(self) -> AgentModelSpec:
        if self.recommended_effort is not None and self.recommended_effort not in self.efforts:
            raise ValueError("recommended effort must be supported by the model")
        return self


class AgentModelCatalog(AgentModel):
    runtime_name: NonBlank
    runtime_id: NonBlank
    provider_id: NonBlank
    revision: NonBlank
    models: tuple[AgentModelSpec, ...]

    @model_validator(mode="after")
    def model_ids_must_be_unique(self) -> AgentModelCatalog:
        model_ids = [model.id for model in self.models]
        if not model_ids:
            raise ValueError("model catalog must contain at least one selectable model")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("model catalog contains duplicate model IDs")
        return self


class AgentErrorInfo(AgentModel):
    code: NonBlank
    message: NonBlank
    retryable: bool = False


class AgentRuntimeDescription(AgentModel):
    name: NonBlank
    runtime_id: NonBlank
    available: bool
    capabilities: AgentRuntimeCapabilities
    catalog_revision: str | None = None
    metadata: JsonObject = Field(default_factory=dict)
    error: AgentErrorInfo | None = None

    @model_validator(mode="after")
    def availability_must_match_error(self) -> AgentRuntimeDescription:
        if self.available and self.error is not None:
            raise ValueError("available runtime cannot include an availability error")
        if not self.available and self.error is None:
            raise ValueError("unavailable runtime must include an error")
        return self


class AgentExecutionIdentity(AgentModel):
    execution_id: NonBlank
    workflow_instance_id: NonBlank
    node_id: NonBlank
    activation: int = Field(ge=1)
    attempt: int = Field(ge=1)
    idempotency_key: NonBlank


class WorkspaceLease(AgentModel):
    lease_id: NonBlank
    workspace_id: NonBlank
    root: Path
    access_mode: Literal["read_only", "workspace_write"]
    isolated: bool
    worktree_id: NonBlank | None = None

    @field_validator("root")
    @classmethod
    def root_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace lease root must be absolute")
        return value

    @model_validator(mode="after")
    def isolated_write_lease_requires_worktree(self) -> WorkspaceLease:
        if self.access_mode == "workspace_write" and not self.isolated:
            raise ValueError("workspace-write leases must be isolated")
        if self.worktree_id is not None and not self.isolated:
            raise ValueError("worktree ID is only valid for isolated workspaces")
        return self


class ArtifactRef(AgentModel):
    artifact_id: NonBlank
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: NonBlank


class AgentPolicyContext(AgentModel):
    sandbox_mode: Literal["read_only", "workspace_write"]
    approval_mode: Literal["deny_all", "auto_review"] = "deny_all"
    network_policy: Literal["deny", "agent_default"] = "deny"
    allowed_tool_profile: NonBlank = "coding-default"


class AgentRequestBase(AgentModel):
    identity: AgentExecutionIdentity
    provider: NonBlank
    model: NonBlank
    effort: EffortName
    workspace: WorkspaceLease
    prompt: PromptText
    resolved_inputs: JsonObject = Field(default_factory=dict)
    input_artifacts: tuple[ArtifactRef, ...] = ()
    output_schema: StrictSchemaIr
    timeout_ms: int = Field(gt=0, le=86_400_000)
    policy: AgentPolicyContext

    @field_validator("provider", "model", "effort")
    @classmethod
    def selection_must_be_explicit(cls, value: str) -> str:
        return _non_blank(value)

    @model_validator(mode="after")
    def workspace_policy_must_match(self) -> AgentRequestBase:
        if self.workspace.access_mode != self.policy.sandbox_mode:
            raise ValueError("workspace access mode and sandbox policy must match")
        return self


class AgentStartRequest(AgentRequestBase):
    session_mode: Literal["new"] = "new"


class AgentResumeRequest(AgentRequestBase):
    session_mode: Literal["resume"] = "resume"
    provider_session_id: NonBlank

    @field_validator("provider_session_id")
    @classmethod
    def session_id_must_not_be_blank(cls, value: str) -> str:
        return _non_blank(value)


type AgentExecutionRequest = AgentStartRequest | AgentResumeRequest


def logical_agent_request_key(request: AgentExecutionRequest) -> str:
    logical_identity = request.identity.model_copy(update={"attempt": 1})
    logical_request = request.model_copy(update={"identity": logical_identity})
    return logical_request.model_dump_json()


class PreparedAgentSession(AgentModel):
    handle_id: NonBlank
    execution_id: NonBlank
    provider: NonBlank
    provider_session_id: NonBlank


class AgentTurnHandle(AgentModel):
    handle_id: NonBlank
    execution_id: NonBlank
    provider: NonBlank
    provider_session_id: NonBlank
    provider_turn_id: NonBlank


class AgentEventBase(AgentModel):
    schema_version: Literal[1] = 1
    execution_id: NonBlank
    sequence: int = Field(ge=1)
    provider_session_id: NonBlank
    summary: str = Field(default="", max_length=4_096)
    native_event_type: str | None = Field(default=None, max_length=256)
    raw_artifact_id: NonBlank | None = None


class AgentStartedEvent(AgentEventBase):
    kind: Literal["started"] = "started"
    model: NonBlank
    effort: EffortName


class AgentMessageDeltaEvent(AgentEventBase):
    kind: Literal["message_delta"] = "message_delta"
    text: str


class AgentMessageCompletedEvent(AgentEventBase):
    kind: Literal["message_completed"] = "message_completed"
    text: str


class AgentToolStartedEvent(AgentEventBase):
    kind: Literal["tool_started"] = "tool_started"
    tool_name: NonBlank
    call_id: NonBlank


class AgentToolCompletedEvent(AgentEventBase):
    kind: Literal["tool_completed"] = "tool_completed"
    tool_name: NonBlank
    call_id: NonBlank


class AgentUsageEvent(AgentEventBase):
    kind: Literal["usage"] = "usage"
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class AgentWarningEvent(AgentEventBase):
    kind: Literal["warning"] = "warning"
    code: NonBlank


class AgentCompletedEvent(AgentEventBase):
    kind: Literal["completed"] = "completed"
    output: JsonObject


class AgentFailedEvent(AgentEventBase):
    kind: Literal["failed"] = "failed"
    error: AgentErrorInfo


class AgentCancelledEvent(AgentEventBase):
    kind: Literal["cancelled"] = "cancelled"
    reason: str | None = None


type AgentEvent = Annotated[
    AgentStartedEvent
    | AgentMessageDeltaEvent
    | AgentMessageCompletedEvent
    | AgentToolStartedEvent
    | AgentToolCompletedEvent
    | AgentUsageEvent
    | AgentWarningEvent
    | AgentCompletedEvent
    | AgentFailedEvent
    | AgentCancelledEvent,
    Field(discriminator="kind"),
]

type TerminalAgentEvent = AgentCompletedEvent | AgentFailedEvent | AgentCancelledEvent


class CancelResult(AgentModel):
    execution_id: NonBlank
    status: Literal["requested", "already_terminal", "not_found"]


class AgentReconcileRequest(AgentModel):
    execution_id: NonBlank
    provider_session_id: NonBlank | None = None
    provider_turn_id: NonBlank | None = None
    phase: Literal["prepared", "starting", "running", "finalizing", "unknown"] = "unknown"
    last_sequence: int = Field(default=0, ge=0)


class ReconcileResult(AgentModel):
    execution_id: NonBlank
    status: Literal[
        "not_found",
        "prepared",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "uncertain",
    ]
    provider_session_id: NonBlank | None = None
    provider_turn_id: NonBlank | None = None
    attachable: bool = False
    last_sequence: int = Field(default=0, ge=0)
    output: JsonObject | None = None
    error: AgentErrorInfo | None = None

    @model_validator(mode="after")
    def fields_must_match_status(self) -> ReconcileResult:
        if self.status == "succeeded" and self.output is None:
            raise ValueError("succeeded reconciliation requires output")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed reconciliation requires error")
        if self.status not in {"prepared", "running"} and self.attachable:
            raise ValueError("only prepared or running executions can be attachable")
        if self.status in {"not_found", "uncertain"} and self.output is not None:
            raise ValueError("non-terminal reconciliation cannot include output")
        return self


TERMINAL_EVENT_KINDS = frozenset({"completed", "failed", "cancelled"})
