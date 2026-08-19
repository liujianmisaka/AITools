from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from misaka_kernel_contracts import ContractError, JsonObject


class PrincipalKind(StrEnum):
    HUMAN = "human"
    APPLICATION = "application"
    AGENT = "agent"
    SERVICE = "service"
    SYSTEM = "system"


class PrincipalRole(StrEnum):
    INITIATOR = "initiator"
    CONTROLLER = "controller"
    OBSERVER = "observer"
    APPROVER = "approver"
    PARTICIPANT = "participant"


@dataclass(frozen=True, slots=True)
class PrincipalRef:
    principal_id: str
    kind: PrincipalKind
    display_name: str = ""

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ContractError(
                "principal.id_empty",
                "principal id must not be empty",
            )
        if self.display_name and not self.display_name.strip():
            raise ContractError(
                "principal.display_name_empty",
                "principal display name must not be whitespace",
            )


@dataclass(frozen=True, slots=True)
class ScopeRef:
    scope_id: str
    parent_scope_id: str | None = None

    def __post_init__(self) -> None:
        if not self.scope_id.strip():
            raise ContractError("scope.id_empty", "scope id must not be empty")
        if self.parent_scope_id is not None and not self.parent_scope_id.strip():
            raise ContractError(
                "scope.parent_id_empty",
                "parent scope id must not be empty when provided",
            )


@dataclass(frozen=True, slots=True)
class PrincipalBinding:
    principal: PrincipalRef
    role: PrincipalRole


@dataclass(frozen=True, slots=True)
class InteractionChannelRef:
    channel_id: str
    scope: ScopeRef

    def __post_init__(self) -> None:
        if not self.channel_id.strip():
            raise ContractError(
                "interaction.channel_id_empty",
                "interaction channel id must not be empty",
            )


class MessageType(StrEnum):
    INSTRUCTION = "instruction"
    QUESTION = "question"
    ANSWER = "answer"
    PROGRESS = "progress"
    ARTIFACT = "artifact"
    RESULT = "result"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESPONSE = "approval_response"
    STEER = "steer"
    CANCEL = "cancel"
    ACK = "ack"


class MessageDeliveryStatus(StrEnum):
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    PROCESSED = "processed"
    COMPLETED = "completed"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class InteractionMessage:
    message_id: str
    channel_id: str
    sender: PrincipalRef
    message_type: MessageType
    payload: JsonObject
    sequence: int
    scope: ScopeRef
    recipient: PrincipalRef | None = None
    payload_schema: JsonObject | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    reply_to: str | None = None
    delivery_status: MessageDeliveryStatus = MessageDeliveryStatus.ACCEPTED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name, value in {
            "message_id": self.message_id,
            "channel_id": self.channel_id,
        }.items():
            if not value.strip():
                raise ContractError(
                    f"interaction.{field_name}_empty",
                    f"{field_name} must not be empty",
                )
        if self.sequence < 1:
            raise ContractError(
                "interaction.sequence_invalid",
                "message sequence must be at least one",
            )
        _require_aware(self.created_at, "interaction.created_at_naive")
        if self.expires_at is not None:
            _require_aware(self.expires_at, "interaction.expires_at_naive")
            if self.expires_at <= self.created_at:
                raise ContractError(
                    "interaction.expiry_invalid",
                    "message expiry must be after creation time",
                )
        for field_name, value in {
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "reply_to": self.reply_to,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"interaction.{field_name}_empty",
                    f"{field_name} must not be whitespace when provided",
                )


@dataclass(frozen=True, slots=True)
class MessageCursor:
    channel_id: str
    next_sequence: int = 1

    def __post_init__(self) -> None:
        if not self.channel_id.strip():
            raise ContractError(
                "interaction.cursor_channel_empty",
                "cursor channel id must not be empty",
            )
        if self.next_sequence < 1:
            raise ContractError(
                "interaction.cursor_sequence_invalid",
                "cursor next sequence must be at least one",
            )


class DecisionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class DecisionRef:
    proposal_id: str
    revision: int

    def __post_init__(self) -> None:
        if not self.proposal_id.strip():
            raise ContractError(
                "decision.proposal_id_empty",
                "decision proposal id must not be empty",
            )
        if self.revision < 1:
            raise ContractError(
                "decision.revision_invalid",
                "decision revision must be at least one",
            )


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    ref: DecisionRef
    plan_hash: str
    requested_effects: tuple[str, ...]
    scope: ScopeRef
    created_by: PrincipalRef
    payload: JsonObject = field(default_factory=dict)
    policy_snapshot: JsonObject = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.plan_hash.strip():
            raise ContractError("decision.plan_hash_empty", "plan hash must not be empty")
        if any(not effect.strip() for effect in self.requested_effects):
            raise ContractError(
                "decision.effect_empty",
                "requested effects must not contain empty values",
            )
        if len(self.requested_effects) != len(set(self.requested_effects)):
            raise ContractError(
                "decision.effect_duplicate",
                "requested effects must be unique",
            )
        _require_aware(self.created_at, "decision.created_at_naive")


@dataclass(frozen=True, slots=True)
class DecisionFact:
    ref: DecisionRef
    status: DecisionStatus
    decided_by: PrincipalRef
    reason: str = ""
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.status is DecisionStatus.PENDING:
            raise ContractError(
                "decision.fact_pending",
                "decision fact must be terminal",
            )
        if self.status is DecisionStatus.REJECTED and not self.reason.strip():
            raise ContractError(
                "decision.rejection_reason_empty",
                "rejected decisions must include a reason",
            )
        _require_aware(self.decided_at, "decision.decided_at_naive")


def _require_aware(value: datetime, code: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(code, "timestamp must be timezone-aware")
