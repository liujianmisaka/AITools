from __future__ import annotations

from typing import Any, Literal

from misaka_delegation_contracts import DelegationMode
from misaka_interaction_contracts import MessageType, PrincipalKind
from pydantic import BaseModel, ConfigDict, Field


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
    instance_id: str
    plan_hash: str
    requested_effects: list[str]
    scope_id: str
    status: str
    decided_by: str | None = None
    reason: str | None = None
    created_at: str
    decided_at: str | None = None


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


class DelegationSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: PrincipalSubmission
    delegation_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    initiator: PrincipalSubmission
    controller: PrincipalSubmission
    scope: ScopeSubmission
    capability_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    input: dict[str, Any]
    provider_id: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    effort: str | None = Field(default=None, min_length=1)
    output_schema: dict[str, Any] | None = None
    mode: DelegationMode = DelegationMode.ONE_SHOT
    parent_delegation_id: str | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    channel_id: str | None = Field(default=None, min_length=1)
    decision_ref: DecisionRefSubmission | None = None
    required_features: set[str] = Field(default_factory=set)
    constraints: dict[str, Any] = Field(default_factory=dict)
    observers: list[PrincipalSubmission] = Field(default_factory=list)
    policy: DelegationPolicySubmission = Field(default_factory=DelegationPolicySubmission)


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
