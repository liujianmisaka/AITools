from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from misaka_interaction_contracts import (
    DecisionRef,
    PrincipalRef,
    ScopeRef,
)
from misaka_kernel_contracts import ContractError, JsonObject, JsonValue


class DelegationMode(StrEnum):
    ONE_SHOT = "one_shot"
    CONTINUABLE = "continuable"


class DelegationStatus(StrEnum):
    PROPOSED = "proposed"
    ADMITTED = "admitted"
    PREPARING = "preparing"
    ACTIVE = "active"
    WAITING_INPUT = "waiting_input"
    REPORTING = "reporting"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RECONCILING = "reconciling"


class ContinuationOperation(StrEnum):
    PREPARE = "prepare"
    START = "start"
    FOLLOW_UP = "follow_up"
    REPLY = "reply"
    STEER = "steer"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    CLOSE = "close"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class DelegationBudget:
    """Monotonic limits copied into a delegation at admission time."""

    max_depth: int = 8
    fan_out_limit: int = 8
    max_concurrent_children: int = 4
    max_activations: int = 16
    time_budget_seconds: float | None = None
    resource_budget: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in {
            "max_depth": self.max_depth,
            "fan_out_limit": self.fan_out_limit,
            "max_concurrent_children": self.max_concurrent_children,
            "max_activations": self.max_activations,
        }.items():
            if isinstance(value, bool) or value < 0:
                raise ContractError(
                    f"delegation.{field_name}_invalid",
                    f"{field_name} must not be negative",
                )
        if self.fan_out_limit < 1 or self.max_concurrent_children < 1 or self.max_activations < 1:
            raise ContractError(
                "delegation.budget_limit_invalid",
                "fan_out_limit, max_concurrent_children and max_activations must be at least one",
            )
        if self.time_budget_seconds is not None and self.time_budget_seconds <= 0:
            raise ContractError(
                "delegation.time_budget_invalid",
                "time budget must be positive when provided",
            )


@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    """The child policy requested by a Delegation Intent."""

    child_scope: ScopeRef | None = None
    budget: DelegationBudget = field(default_factory=DelegationBudget)
    tool_allowlist: frozenset[str] = frozenset()
    tool_denylist: frozenset[str] = frozenset()
    persona: str | None = None
    requested_effects: tuple[str, ...] = ()
    require_decision: bool = False

    def __post_init__(self) -> None:
        for name, values in {
            "tool_allowlist": self.tool_allowlist,
            "tool_denylist": self.tool_denylist,
        }.items():
            if any(not value.strip() for value in values):
                raise ContractError(
                    f"delegation.{name}_empty",
                    f"{name} must not contain empty values",
                )
        if self.tool_allowlist & self.tool_denylist:
            raise ContractError(
                "delegation.tool_policy_conflict",
                "a tool cannot be both allowed and denied",
            )
        if self.persona is not None and not self.persona.strip():
            raise ContractError(
                "delegation.persona_empty",
                "persona must not be whitespace when provided",
            )
        if any(not effect.strip() for effect in self.requested_effects):
            raise ContractError(
                "delegation.effect_empty",
                "requested effects must not contain empty values",
            )
        if len(self.requested_effects) != len(set(self.requested_effects)):
            raise ContractError(
                "delegation.effect_duplicate",
                "requested effects must be unique",
            )


@dataclass(frozen=True, slots=True)
class DelegationAdmission:
    """The durable decision made before a child Activation is published."""

    allowed: bool
    reason: str
    decision_ref: DecisionRef | None = None
    policy_snapshot: JsonObject = field(default_factory=dict)
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ContractError(
                "delegation.admission_reason_empty",
                "admission reason must not be empty",
            )
        if self.allowed and self.error_code is not None:
            raise ContractError(
                "delegation.admission_error_on_allow",
                "an allowed admission cannot contain an error code",
            )
        if self.error_code is not None and not self.error_code.strip():
            raise ContractError(
                "delegation.admission_error_empty",
                "admission error code must not be whitespace",
            )


@dataclass(frozen=True, slots=True)
class DelegationRef:
    delegation_id: str
    session_id: str | None = None
    channel_id: str | None = None
    parent_delegation_id: str | None = None
    depth: int = 0
    child_scope: ScopeRef | None = None

    def __post_init__(self) -> None:
        if not self.delegation_id.strip():
            raise ContractError(
                "delegation.id_empty",
                "delegation id must not be empty",
            )
        for field_name, value in {
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "parent_delegation_id": self.parent_delegation_id,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"delegation.{field_name}_empty",
                    f"{field_name} must not be whitespace when provided",
                )
        if isinstance(self.depth, bool) or self.depth < 0:
            raise ContractError(
                "delegation.depth_invalid",
                "delegation depth must not be negative",
            )


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    delegation_id: str
    idempotency_key: str
    initiator: PrincipalRef
    controller: PrincipalRef
    scope: ScopeRef
    capability_id: str
    operation: str
    input: JsonObject
    provider_id: str | None = None
    model: str | None = None
    effort: str | None = None
    output_schema: JsonObject | None = None
    mode: DelegationMode = DelegationMode.ONE_SHOT
    parent_delegation_id: str | None = None
    session_id: str | None = None
    channel_id: str | None = None
    decision_ref: DecisionRef | None = None
    required_features: frozenset[str] = frozenset()
    constraints: JsonObject = field(default_factory=dict)
    observers: tuple[PrincipalRef, ...] = ()
    policy: DelegationPolicy = field(default_factory=DelegationPolicy)

    def __post_init__(self) -> None:
        for field_name, value in {
            "delegation_id": self.delegation_id,
            "idempotency_key": self.idempotency_key,
            "capability_id": self.capability_id,
            "operation": self.operation,
        }.items():
            if not value.strip():
                raise ContractError(
                    f"delegation.{field_name}_empty",
                    f"{field_name} must not be empty",
                )
        for field_name, value in {
            "parent_delegation_id": self.parent_delegation_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "effort": self.effort,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"delegation.{field_name}_empty",
                    f"{field_name} must not be whitespace when provided",
                )
        for feature in self.required_features:
            if not feature.strip():
                raise ContractError(
                    "delegation.feature_empty",
                    "required features must not contain empty values",
                )
        observer_ids = [observer.principal_id for observer in self.observers]
        if len(observer_ids) != len(set(observer_ids)):
            raise ContractError(
                "delegation.observer_duplicate",
                "delegation observers must be unique",
            )


@dataclass(frozen=True, slots=True)
class DelegationIntent:
    """Immutable domain intent separated from transport/request wrappers."""

    intent_id: str
    request: DelegationRequest

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ContractError(
                "delegation.intent_id_empty",
                "delegation intent id must not be empty",
            )

    @property
    def delegation_id(self) -> str:
        return self.request.delegation_id


@dataclass(frozen=True, slots=True)
class ContinuationRequest:
    request_id: str
    delegation_id: str
    operation: ContinuationOperation
    actor: PrincipalRef
    idempotency_key: str
    session_id: str | None = None
    message_id: str | None = None
    expected_activation_id: str | None = None
    input: JsonObject = field(default_factory=dict)
    correlation_id: str | None = None
    reply_to: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in {
            "request_id": self.request_id,
            "delegation_id": self.delegation_id,
            "idempotency_key": self.idempotency_key,
        }.items():
            if not value.strip():
                raise ContractError(
                    f"continuation.{field_name}_empty",
                    f"{field_name} must not be empty",
                )
        if self.session_id is not None and not self.session_id.strip():
            raise ContractError(
                "continuation.session_id_empty",
                "session id must not be whitespace when provided",
            )
        if self.expected_activation_id is not None and not self.expected_activation_id.strip():
            raise ContractError(
                "continuation.activation_id_empty",
                "expected activation id must not be whitespace when provided",
            )
        if self.operation in {
            ContinuationOperation.FOLLOW_UP,
            ContinuationOperation.REPLY,
            ContinuationOperation.STEER,
        }:
            if self.session_id is None or self.message_id is None:
                raise ContractError(
                    "continuation.message_refs_required",
                    "follow-up, reply and steer require session_id and message_id",
                )
        if self.operation is ContinuationOperation.REPLY:
            if self.reply_to is None or self.correlation_id is None:
                raise ContractError(
                    "continuation.reply_refs_required",
                    "reply requires reply_to and correlation_id",
                )
        for field_name, value in {
            "correlation_id": self.correlation_id,
            "reply_to": self.reply_to,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"continuation.{field_name}_empty",
                    f"{field_name} must not be whitespace when provided",
                )
        if self.message_id is not None and not self.message_id.strip():
            raise ContractError(
                "continuation.message_id_empty",
                "message id must not be whitespace when provided",
            )


@dataclass(frozen=True, slots=True)
class DelegationReport:
    delegation_id: str
    status: DelegationStatus
    output: JsonValue | None = None
    artifact_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    source_invocation_id: str | None = None
    source_activation_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.delegation_id.strip():
            raise ContractError(
                "delegation.report_id_empty",
                "delegation report id must not be empty",
            )
        if self.status not in {
            DelegationStatus.COMPLETED,
            DelegationStatus.FAILED,
            DelegationStatus.CANCELLED,
            DelegationStatus.RECONCILIATION_REQUIRED,
            DelegationStatus.REJECTED,
        }:
            raise ContractError(
                "delegation.report_status_non_terminal",
                "delegation report status must be terminal",
            )
        if self.error_code is not None and not self.error_code.strip():
            raise ContractError(
                "delegation.report_error_code_empty",
                "error code must not be whitespace when provided",
            )
        if self.error_message is not None and not self.error_message.strip():
            raise ContractError(
                "delegation.report_error_message_empty",
                "error message must not be whitespace when provided",
            )
        if any(not artifact_id.strip() for artifact_id in self.artifact_ids):
            raise ContractError(
                "delegation.report_artifact_id_empty",
                "artifact ids must not contain empty values",
            )
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ContractError(
                "delegation.report_artifact_duplicate",
                "artifact ids must be unique",
            )
        for field_name, value in {
            "source_invocation_id": self.source_invocation_id,
            "source_activation_id": self.source_activation_id,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"delegation.report_{field_name}_empty",
                    f"{field_name} must not be whitespace when provided",
                )
        if (self.source_invocation_id is None) != (self.source_activation_id is None):
            raise ContractError(
                "delegation.report_execution_identity_incomplete",
                "delegation report invocation and activation ids must be provided together",
            )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ContractError(
                "delegation.report_timestamp_naive",
                "delegation report timestamp must be timezone-aware",
            )


@dataclass(frozen=True, slots=True)
class DelegationSnapshot:
    ref: DelegationRef
    request: DelegationRequest
    status: DelegationStatus
    revision: int = 1
    child_refs: tuple[DelegationRef, ...] = ()
    report: DelegationReport | None = None
    report_history: tuple[DelegationReport, ...] = ()
    current_invocation_id: str | None = None
    current_activation_id: str | None = None
    activation_count: int = 0
    admission: DelegationAdmission | None = None
    intent: DelegationIntent | None = None

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ContractError(
                "delegation.revision_invalid",
                "delegation revision must be at least one",
            )
        if self.activation_count < 0:
            raise ContractError(
                "delegation.activation_count_invalid",
                "delegation activation count must not be negative",
            )
        for field_name, value in {
            "current_invocation_id": self.current_invocation_id,
            "current_activation_id": self.current_activation_id,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"delegation.{field_name}_empty",
                    f"{field_name} must not be whitespace when provided",
                )
        if (self.current_invocation_id is None) != (self.current_activation_id is None):
            raise ContractError(
                "delegation.current_execution_identity_incomplete",
                "current invocation and activation ids must be provided together",
            )
        if self.ref.delegation_id != self.request.delegation_id:
            raise ContractError(
                "delegation.snapshot_id_mismatch",
                "snapshot ref and request delegation ids must match",
            )
        if self.intent is not None:
            if self.intent.delegation_id != self.ref.delegation_id:
                raise ContractError(
                    "delegation.intent_id_mismatch",
                    "delegation intent must belong to the snapshot delegation",
                )
            if self.intent.request != self.request:
                raise ContractError(
                    "delegation.intent_request_mismatch",
                    "delegation intent request must match the snapshot request",
                )
        child_ids = [child.delegation_id for child in self.child_refs]
        if len(child_ids) != len(set(child_ids)):
            raise ContractError(
                "delegation.child_duplicate",
                "delegation child ids must be unique",
            )
        if self.report is not None and self.report.delegation_id != self.ref.delegation_id:
            raise ContractError(
                "delegation.report_id_mismatch",
                "delegation report id must match snapshot ref",
            )
        if any(report.delegation_id != self.ref.delegation_id for report in self.report_history):
            raise ContractError(
                "delegation.report_history_id_mismatch",
                "delegation report history must belong to snapshot ref",
            )
        if (
            self.admission is not None
            and self.admission.allowed is False
            and self.status
            not in {
                DelegationStatus.PROPOSED,
                DelegationStatus.REJECTED,
                DelegationStatus.FAILED,
            }
        ):
            raise ContractError(
                "delegation.admission_status_mismatch",
                "a denied admission cannot have a live delegation status",
            )
