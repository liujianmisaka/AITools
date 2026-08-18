from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from misaka_invocation_contracts import (
    ArtifactRef,
    CapabilityFeature,
    SessionRef,
)
from misaka_kernel_contracts import ContractError, JsonObject, JsonValue


class TaskStatus(StrEnum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    CANCELLING = "cancelling"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.REJECTED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.RECONCILIATION_REQUIRED,
    }
)


@dataclass(frozen=True, slots=True)
class A2ASkill:
    skill_id: str
    name: str
    description: str
    capability_id: str
    operation: str
    input_schema: JsonObject = field(default_factory=dict)
    output_schema: JsonObject = field(default_factory=dict)
    features: frozenset[CapabilityFeature] = frozenset()

    def __post_init__(self) -> None:
        for field_name, value in {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "capability_id": self.capability_id,
            "operation": self.operation,
        }.items():
            if not value.strip():
                raise ContractError(
                    f"a2a.skill_{field_name}_empty",
                    f"A2A skill {field_name} must not be empty",
                )


@dataclass(frozen=True, slots=True)
class A2AAgentCard:
    agent_id: str
    name: str
    description: str
    version: str
    skills: tuple[A2ASkill, ...]
    features: frozenset[CapabilityFeature] = frozenset()
    max_input_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        for field_name, value in {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
        }.items():
            if not value.strip():
                raise ContractError(
                    f"a2a.card_{field_name}_empty",
                    f"A2A card {field_name} must not be empty",
                )
        if not self.skills:
            raise ContractError("a2a.card_skills_empty", "A2A card must declare skills")
        skill_ids = [skill.skill_id for skill in self.skills]
        if len(skill_ids) != len(set(skill_ids)):
            raise ContractError(
                "a2a.card_skill_duplicate",
                "A2A card skill ids must be unique",
            )
        if self.max_input_bytes < 1:
            raise ContractError(
                "a2a.card_input_limit_invalid",
                "A2A card max input bytes must be positive",
            )


@dataclass(frozen=True, slots=True)
class TaskRequest:
    task_id: str
    context_id: str
    message_id: str
    idempotency_key: str
    capability_id: str
    operation: str
    input: JsonObject
    provider_id: str | None = None
    model: str | None = None
    effort: str | None = None
    session_ref: SessionRef | None = None
    required_features: frozenset[CapabilityFeature] = frozenset()
    output_schema: JsonObject | None = None
    policy_context: JsonObject = field(default_factory=dict)
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in {
            "task_id": self.task_id,
            "context_id": self.context_id,
            "message_id": self.message_id,
            "idempotency_key": self.idempotency_key,
            "capability_id": self.capability_id,
            "operation": self.operation,
        }.items():
            if not value.strip():
                raise ContractError(
                    f"a2a.task_{field_name}_empty",
                    f"A2A task {field_name} must not be empty",
                )
        for field_name, value in {
            "provider_id": self.provider_id,
            "model": self.model,
            "effort": self.effort,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"a2a.task_{field_name}_empty",
                    f"A2A task {field_name} must not be empty when provided",
                )


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    invocation_id: str | None
    status: TaskStatus
    output: JsonValue | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ContractError("a2a.result_task_id_empty", "task id must not be empty")
        if self.invocation_id is not None and not self.invocation_id.strip():
            raise ContractError(
                "a2a.result_invocation_id_empty",
                "invocation id must not be empty when provided",
            )
        if self.status not in TERMINAL_TASK_STATUSES:
            raise ContractError(
                "a2a.result_status_non_terminal",
                "A2A task result must be terminal",
            )


@dataclass(frozen=True, slots=True)
class TaskEvent:
    task_id: str
    sequence: int
    status: TaskStatus
    payload: JsonObject = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ContractError("a2a.event_task_id_empty", "task id must not be empty")
        if self.sequence < 1:
            raise ContractError(
                "a2a.event_sequence_invalid",
                "task event sequence must be positive",
            )
        if self.occurred_at.tzinfo is None:
            raise ContractError(
                "a2a.event_timestamp_naive",
                "task event timestamp must be timezone-aware",
            )


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    request: TaskRequest
    fingerprint: str
    status: TaskStatus
    invocation_id: str | None
    events: tuple[TaskEvent, ...]
    result: TaskResult | None


def task_request_fingerprint(request: TaskRequest) -> str:
    payload = {
        "context_id": request.context_id,
        "message_id": request.message_id,
        "idempotency_key": request.idempotency_key,
        "capability_id": request.capability_id,
        "operation": request.operation,
        "input": request.input,
        "provider_id": request.provider_id,
        "model": request.model,
        "effort": request.effort,
        "session_ref": (
            {"provider": request.session_ref.provider, "native_id": request.session_ref.native_id}
            if request.session_ref is not None
            else None
        ),
        "required_features": sorted(feature.value for feature in request.required_features),
        "output_schema": request.output_schema,
        "policy_context": request.policy_context,
        "metadata": request.metadata,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
