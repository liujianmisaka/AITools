from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal, cast

from misaka_approval_capability import DecisionRecord
from misaka_delegation_contracts import DelegationMode
from misaka_interaction_contracts import MessageType, PrincipalKind
from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    input: dict[str, Any]
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    network_policy: Literal["allow", "deny"] = "deny"
    provider_id: str | None = Field(default=None, min_length=1)
    output_schema: dict[str, Any] | None = None
    max_attempts: int = Field(default=1, ge=1)


class JobView(BaseModel):
    job_id: str
    idempotency_key: str
    status: str
    version: int
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


class HealthView(BaseModel):
    status: str
    profile: str


class ServiceIndexView(BaseModel):
    service: str
    profile: str
    version: str
    status: str
    description: str
    links: dict[str, str]


class ServiceView(BaseModel):
    service_id: str
    display_name: str
    description: str
    category: str
    status: str
    controllable: bool
    endpoint: str | None = None
    pid: int | None = None
    process_create_time: float | None = None
    epoch: int = 0
    started_at: str | None = None
    stopped_at: str | None = None
    exit_code: int | None = None
    last_error: str | None = None
    recent_output: list[str] = Field(default_factory=list)


class CapabilityView(BaseModel):
    capability_id: str
    version: str
    operations: list[str]
    features: list[str]


class ModelView(BaseModel):
    model_id: str
    display_name: str
    description: str
    supported_efforts: list[str]


class ModelCatalogView(BaseModel):
    provider_id: str
    models: list[ModelView]


class TemplateNodeSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    input: dict[str, Any]
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    network_policy: Literal["allow", "deny"] = "deny"
    provider_id: str | None = Field(default=None, min_length=1)
    output_schema: dict[str, Any] | None = None
    depends_on: list[str] = Field(default_factory=list)


class TemplateSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    name: str = Field(min_length=1)
    coordinator: Literal["direct", "dag"]
    nodes: list[TemplateNodeSubmission] = Field(min_length=1)
    decision_required: bool = False


class TemplateView(TemplateSubmission):
    created_at: str


class InstanceSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)


class InstanceView(BaseModel):
    instance_id: str
    idempotency_key: str
    template_id: str
    template_version: int
    status: str
    version: int
    input: dict[str, Any]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str


class TriggerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    template_version: int = Field(ge=1)
    enabled: bool = True


class TriggerView(TriggerSubmission):
    created_at: str


class EventSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class EventDeliveryView(BaseModel):
    event_id: str
    event_type: str
    instance_ids: list[str]


class DecisionSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    principal_id: str = Field(min_length=1)
    reason: str = Field(default="", max_length=2000)


class DecisionView(BaseModel):
    proposal_id: str
    revision: int
    instance_id: str | None = None
    delegation_id: str | None = None
    plan_hash: str
    requested_effects: list[str]
    scope_id: str
    status: str
    decided_by: str | None = None
    reason: str | None = None
    created_at: str
    decided_at: str | None = None

    @classmethod
    def from_record(cls, record: DecisionRecord) -> DecisionView:
        instance_id = record.proposal.payload.get("instance_id")
        delegation_id = record.proposal.payload.get("delegation_id")
        return cls(
            proposal_id=record.proposal.ref.proposal_id,
            revision=record.proposal.ref.revision,
            instance_id=(
                instance_id if isinstance(instance_id, str) and instance_id.strip() else None
            ),
            delegation_id=(
                delegation_id if isinstance(delegation_id, str) and delegation_id.strip() else None
            ),
            plan_hash=record.proposal.plan_hash,
            requested_effects=list(record.proposal.requested_effects),
            scope_id=record.proposal.scope.scope_id,
            status=record.status.value,
            decided_by=record.fact.decided_by.principal_id if record.fact else None,
            reason=record.fact.reason if record.fact else None,
            created_at=record.proposal.created_at.isoformat(),
            decided_at=record.fact.decided_at.isoformat() if record.fact else None,
        )


class PrincipalSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: str = Field(min_length=1)
    kind: PrincipalKind
    display_name: str = ""


class ScopeSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1)
    parent_scope_id: str | None = Field(default=None, min_length=1)


class DecisionRefSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1)
    revision: int = Field(ge=1)


class DelegationBudgetSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_depth: int = Field(default=8, ge=0)
    fan_out_limit: int = Field(default=8, ge=1)
    max_concurrent_children: int = Field(default=4, ge=1)
    max_activations: int = Field(default=16, ge=1)
    time_budget_seconds: float | None = Field(default=None, gt=0)
    resource_budget: dict[str, Any] = Field(default_factory=dict)


class DelegationPolicySubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child_scope: ScopeSubmission | None = None
    budget: DelegationBudgetSubmission = Field(default_factory=DelegationBudgetSubmission)
    tool_allowlist: set[str] = Field(default_factory=set)
    tool_denylist: set[str] = Field(default_factory=set)
    persona: str | None = Field(default=None, min_length=1)
    requested_effects: list[str] = Field(default_factory=list)
    require_decision: bool = False


class DelegationSpecSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: PrincipalSubmission
    initiator: PrincipalSubmission
    controller: PrincipalSubmission
    scope: ScopeSubmission
    capability_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    input: dict[str, Any]
    cwd: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    policy_context: dict[str, Any]
    output_schema: dict[str, Any] | None
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: DelegationMode = DelegationMode.ONE_SHOT
    parent_delegation_id: str | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    channel_id: str | None = Field(default=None, min_length=1)
    decision_ref: DecisionRefSubmission | None
    required_features: set[str] = Field(default_factory=set)
    observers: list[PrincipalSubmission] = Field(default_factory=list)
    policy: DelegationPolicySubmission = Field(default_factory=DelegationPolicySubmission)

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_gateway_json(value, path="input", forbid_sandbox=True)
        return value

    @field_validator("policy_context")
    @classmethod
    def validate_policy_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_gateway_json(value, path="policy_context", forbid_sandbox=False)
        unknown = set(value) - {"sandbox", "network_policy"}
        if unknown:
            raise ValueError(
                f"policy_context contains unsupported fields: {', '.join(sorted(unknown))}"
            )
        sandbox = value.get("sandbox", "read_only")
        network_policy = value.get("network_policy", "deny")
        if sandbox not in {"read_only", "workspace_write"}:
            raise ValueError("policy_context.sandbox must be read_only or workspace_write")
        if network_policy not in {"allow", "deny"}:
            raise ValueError("policy_context.network_policy must be allow or deny")
        return {"sandbox": sandbox, "network_policy": network_policy}

    @field_validator("output_schema")
    @classmethod
    def validate_output_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            _validate_json_value(value, path="output_schema")
        return value


class DelegationSubmission(DelegationSpecSubmission):
    delegation_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class DelegationTriggerEventSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    sequence: int = Field(default=0, ge=0)
    specversion: Literal["1.0"] = "1.0"
    subject: str | None = Field(default=None, min_length=1)
    datacontenttype: Literal["application/json"] = "application/json"
    occurred_at: datetime | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id", "source", "event_type")
    @classmethod
    def validate_identity_field(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event identity fields must not be blank")
        return value

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("event.subject must not be blank")
        return value

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_gateway_json(value, path="event.data", forbid_sandbox=True)
        return value

    @field_validator("extensions")
    @classmethod
    def validate_extensions(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_gateway_json(value, path="event.extensions", forbid_sandbox=True)
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("event.occurred_at must be timezone-aware")
        return value


class DelegationTriggerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_id: str = Field(min_length=1)
    event: DelegationTriggerEventSubmission
    delegation: DelegationSpecSubmission

    @field_validator("trigger_id")
    @classmethod
    def validate_trigger_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("trigger_id must not be blank")
        return value


class DelegationApprovalSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: PrincipalSubmission
    decision_ref: DecisionRefSubmission
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=2000)


class DelegationMessageSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: PrincipalSubmission
    message_id: str = Field(min_length=1)
    message_type: MessageType
    payload: dict[str, Any]
    recipient: PrincipalSubmission | None = None
    payload_schema: dict[str, Any] | None = None
    correlation_id: str | None = Field(default=None, min_length=1)
    causation_id: str | None = Field(default=None, min_length=1)
    reply_to: str | None = Field(default=None, min_length=1)


class DelegationReplySubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: PrincipalSubmission
    session_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    expected_activation_id: str = Field(min_length=1)
    input: dict[str, Any]
    correlation_id: str = Field(min_length=1)
    reply_to: str = Field(min_length=1)

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_gateway_json(value, path="input", forbid_sandbox=True)
        return value


class DelegationCancelSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: PrincipalSubmission
    session_id: str | None = Field(default=None, min_length=1)
    expected_activation_id: str | None = Field(default=None, min_length=1)
    reason: str = Field(min_length=1, max_length=2000)


class DelegationReconcileSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: PrincipalSubmission
    session_id: str = Field(min_length=1)
    expected_activation_id: str | None = Field(default=None, min_length=1)


class DelegationReportView(BaseModel):
    status: str
    output: Any | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    source_invocation_id: str | None = None
    source_activation_id: str | None = None
    created_at: str


class DelegationView(BaseModel):
    delegation_id: str
    status: str
    revision: int
    session_id: str | None = None
    channel_id: str | None = None
    parent_delegation_id: str | None = None
    depth: int
    child_scope: ScopeSubmission | None = None
    current_invocation_id: str | None = None
    current_activation_id: str | None = None
    activation_count: int
    child_delegation_ids: list[str] = Field(default_factory=list)
    report: DelegationReportView | None = None


class InteractionMessageView(BaseModel):
    message_id: str
    channel_id: str
    sender: PrincipalSubmission
    recipient: PrincipalSubmission | None = None
    message_type: str
    payload: dict[str, Any]
    sequence: int
    scope: ScopeSubmission
    correlation_id: str | None = None
    causation_id: str | None = None
    reply_to: str | None = None
    delivery_status: str
    created_at: str


class DelegationSessionEventView(BaseModel):
    delegation_id: str
    sequence: int
    kind: str
    invocation_id: str | None = None
    activation_id: str | None = None
    activation_number: int | None = None
    status: str | None = None
    provider_session_id: str | None = None
    provider_operation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str


class DelegationSessionView(BaseModel):
    delegation: DelegationView
    provider_id: str | None = None
    model: str | None = None
    effort: str | None = None
    provider_session_id: str | None = None
    provider_operation_id: str | None = None
    activation_number: int
    last_sequence: int
    stage: str | None = None
    closed: bool
    updated_at: str | None = None


_FORBIDDEN_GATEWAY_FIELDS = {
    "apikey",
    "auth",
    "authentication",
    "authorization",
    "client",
    "cmd",
    "command",
    "commandline",
    "credential",
    "credentials",
    "cwd",
    "env",
    "environmentvariables",
    "envvars",
    "environ",
    "environment",
    "executable",
    "password",
    "provider",
    "providerclient",
    "providerobject",
    "providersdk",
    "sdk",
    "secret",
    "shellcommand",
    "token",
    "workdir",
    "workingdirectory",
}
_FORBIDDEN_CREDENTIAL_SUFFIXES = (
    "apikey",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
)


def _validate_gateway_json(value: object, *, path: str, forbid_sandbox: bool) -> None:
    _validate_json_value(value, path=path)
    if isinstance(value, dict):
        for key, nested in cast(dict[str, object], value).items():
            normalized = "".join(character for character in key.casefold() if character.isalnum())
            reserved_name = key.casefold().replace("-", "_")
            if reserved_name.startswith("_misaka_"):
                raise ValueError(f"{path}.{key} is reserved for Gateway metadata")
            if normalized in _FORBIDDEN_GATEWAY_FIELDS or normalized.endswith(
                _FORBIDDEN_CREDENTIAL_SUFFIXES
            ):
                raise ValueError(f"{path}.{key} is not allowed through the Gateway")
            if forbid_sandbox and normalized in {"sandbox", "sandboxmode"}:
                raise ValueError(f"{path}.{key} is owned by Gateway policy")
            _validate_gateway_json(
                nested,
                path=f"{path}.{key}",
                forbid_sandbox=forbid_sandbox,
            )
    elif isinstance(value, list):
        for index, nested in enumerate(cast(list[object], value)):
            _validate_gateway_json(
                nested,
                path=f"{path}[{index}]",
                forbid_sandbox=forbid_sandbox,
            )


def _validate_json_value(value: object, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain non-finite numbers")
        return
    if isinstance(value, list):
        for index, nested in enumerate(cast(list[object], value)):
            _validate_json_value(nested, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, nested in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            _validate_json_value(nested, path=f"{path}.{key}")
        return
    raise ValueError(f"{path} must contain JSON values only")
