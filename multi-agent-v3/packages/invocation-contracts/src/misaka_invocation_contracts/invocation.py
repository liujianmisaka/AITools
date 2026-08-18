from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from misaka_kernel_contracts.errors import ContractError
from misaka_kernel_contracts.events import JsonObject, JsonValue

from misaka_invocation_contracts.capability import CapabilityFeature


class InvocationStatus(StrEnum):
    REGISTERED = "registered"
    PREFLIGHTING = "preflighting"
    REJECTED = "rejected"
    RESOURCE_ACQUIRING = "resource_acquiring"
    PREPARED = "prepared"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ReconcileStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNREACHABLE = "unreachable"


class CompletionBoundary(StrEnum):
    ACCEPTED = "accepted"
    OPERATION_TERMINAL = "operation_terminal"
    SESSION_IDLE = "session_idle"
    ARTIFACT_COMMITTED = "artifact_committed"


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: PolicyEffect
    reason: str
    constraints: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ContractError("policy.reason_empty", "policy reason must not be empty")


@dataclass(frozen=True, slots=True)
class SessionRef:
    provider: str
    native_id: str

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.native_id.strip():
            raise ContractError(
                "session.ref_empty",
                "session provider and native id must not be empty",
            )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    media_type: str
    size_bytes: int
    sha256: str
    location: str
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in {
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "location": self.location,
        }.items():
            if not value.strip():
                raise ContractError(
                    f"artifact.{field_name}_empty",
                    f"artifact {field_name} must not be empty",
                )
        if self.size_bytes < 0:
            raise ContractError(
                "artifact.size_invalid",
                "artifact size must not be negative",
            )


@dataclass(frozen=True, slots=True)
class ActivationRef:
    invocation_id: str
    activation_id: str
    attempt: int = 1

    def __post_init__(self) -> None:
        if not self.invocation_id.strip() or not self.activation_id.strip():
            raise ContractError("activation.ref_empty", "activation ids must not be empty")
        if self.attempt < 1:
            raise ContractError(
                "activation.attempt_invalid",
                "activation attempt must be at least one",
            )


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    invocation_id: str
    capability_id: str
    operation: str
    input: JsonObject
    idempotency_key: str
    completion_boundary: CompletionBoundary
    parent_invocation_id: str | None = None
    session_ref: SessionRef | None = None
    required_features: frozenset[CapabilityFeature] = frozenset()
    output_schema: JsonObject | None = None
    policy_context: JsonObject = field(default_factory=dict)
    attempt: int = 1

    def __post_init__(self) -> None:
        required = {
            "invocation_id": self.invocation_id,
            "capability_id": self.capability_id,
            "operation": self.operation,
            "idempotency_key": self.idempotency_key,
        }
        for field_name, value in required.items():
            if not value.strip():
                raise ContractError(
                    f"invocation.{field_name}_empty",
                    f"{field_name} must not be empty",
                )
        if self.attempt < 1:
            raise ContractError(
                "invocation.attempt_invalid",
                "invocation attempt must be at least one",
            )


@dataclass(frozen=True, slots=True)
class InvocationResult:
    invocation_id: str
    status: InvocationStatus
    output: JsonValue | None = None
    error_code: str | None = None
    error_message: str | None = None
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if not self.invocation_id.strip():
            raise ContractError(
                "result.invocation_id_empty",
                "result invocation id must not be empty",
            )
        if self.status not in {
            InvocationStatus.REJECTED,
            InvocationStatus.SUCCEEDED,
            InvocationStatus.FAILED,
            InvocationStatus.CANCELLED,
            InvocationStatus.RECONCILIATION_REQUIRED,
        }:
            raise ContractError("result.status_non_terminal", "result status must be terminal")


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    status: ReconcileStatus
    message: str | None = None
    provider_operation_id: str | None = None

    def __post_init__(self) -> None:
        if self.message is not None and not self.message.strip():
            raise ContractError(
                "reconcile.message_empty",
                "reconcile message must be non-empty when provided",
            )


@dataclass(frozen=True, slots=True)
class InvocationEvent:
    invocation_id: str
    sequence: int
    status: InvocationStatus
    payload: JsonObject = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.invocation_id.strip():
            raise ContractError(
                "event.invocation_id_empty",
                "event invocation id must not be empty",
            )
        if self.sequence < 1:
            raise ContractError("event.sequence_invalid", "event sequence must be at least one")
        if self.occurred_at.tzinfo is None:
            raise ContractError("event.timestamp_naive", "event timestamp must be timezone-aware")


def request_fingerprint(request: InvocationRequest) -> str:
    payload = {
        "capability_id": request.capability_id,
        "operation": request.operation,
        "input": request.input,
        "idempotency_key": request.idempotency_key,
        "completion_boundary": request.completion_boundary.value,
        "parent_invocation_id": request.parent_invocation_id,
        "session_ref": (
            {"provider": request.session_ref.provider, "native_id": request.session_ref.native_id}
            if request.session_ref is not None
            else None
        ),
        "required_features": sorted(feature.value for feature in request.required_features),
        "output_schema": request.output_schema,
        "policy_context": request.policy_context,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
