from __future__ import annotations

import hashlib
import json
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
    DECIDER = "decider"
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
    DECISION_REQUEST = "decision_request"
    DECISION_RESPONSE = "decision_response"
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
        if self.sequence < 1:
            raise ContractError(
                "interaction.sequence_invalid",
                "message sequence must be at least one",
            )
        _validate_message_fields(
            message_id=self.message_id,
            channel_id=self.channel_id,
            created_at=self.created_at,
            expires_at=self.expires_at,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            reply_to=self.reply_to,
        )


@dataclass(frozen=True, slots=True)
class InteractionMessageDraft:
    """A message before the channel assigns its durable sequence."""

    message_id: str
    channel_id: str
    sender: PrincipalRef
    message_type: MessageType
    payload: JsonObject
    scope: ScopeRef
    recipient: PrincipalRef | None = None
    payload_schema: JsonObject | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    reply_to: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_message_fields(
            message_id=self.message_id,
            channel_id=self.channel_id,
            created_at=self.created_at,
            expires_at=self.expires_at,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            reply_to=self.reply_to,
        )

    def to_message(self, sequence: int) -> InteractionMessage:
        return InteractionMessage(
            message_id=self.message_id,
            channel_id=self.channel_id,
            sender=self.sender,
            message_type=self.message_type,
            payload=self.payload,
            sequence=sequence,
            scope=self.scope,
            recipient=self.recipient,
            payload_schema=self.payload_schema,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            reply_to=self.reply_to,
            created_at=self.created_at,
            expires_at=self.expires_at,
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
    plan_hash: str
    requested_effects: tuple[str, ...]
    scope: ScopeRef
    policy_snapshot: JsonObject
    decided_by: PrincipalRef
    reason: str = ""
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.status is DecisionStatus.PENDING:
            raise ContractError(
                "decision.fact_pending",
                "decision fact must be terminal",
            )
        if not self.plan_hash.strip():
            raise ContractError(
                "decision.fact_plan_hash_empty",
                "decision fact plan hash must not be empty",
            )
        if any(not effect.strip() for effect in self.requested_effects):
            raise ContractError(
                "decision.fact_effect_empty",
                "decision fact effects must not contain empty values",
            )
        if len(self.requested_effects) != len(set(self.requested_effects)):
            raise ContractError(
                "decision.fact_effect_duplicate",
                "decision fact effects must be unique",
            )
        if self.status is DecisionStatus.REJECTED and not self.reason.strip():
            raise ContractError(
                "decision.rejection_reason_empty",
                "rejected decisions must include a reason",
            )
        _require_aware(self.decided_at, "decision.decided_at_naive")

    @classmethod
    def from_proposal(
        cls,
        proposal: DecisionProposal,
        *,
        status: DecisionStatus,
        decided_by: PrincipalRef,
        reason: str = "",
        decided_at: datetime | None = None,
    ) -> DecisionFact:
        return cls(
            ref=proposal.ref,
            status=status,
            plan_hash=proposal.plan_hash,
            requested_effects=proposal.requested_effects,
            scope=proposal.scope,
            policy_snapshot=proposal.policy_snapshot,
            decided_by=decided_by,
            reason=reason,
            decided_at=decided_at or datetime.now(UTC),
        )

    def matches(self, proposal: DecisionProposal) -> bool:
        return (
            self.ref == proposal.ref
            and self.plan_hash == proposal.plan_hash
            and self.requested_effects == proposal.requested_effects
            and self.scope == proposal.scope
            and self.policy_snapshot == proposal.policy_snapshot
        )


def decision_fingerprint(proposal: DecisionProposal) -> str:
    payload = {
        "proposal_id": proposal.ref.proposal_id,
        "revision": proposal.ref.revision,
        "plan_hash": proposal.plan_hash,
        "requested_effects": proposal.requested_effects,
        "scope": {
            "scope_id": proposal.scope.scope_id,
            "parent_scope_id": proposal.scope.parent_scope_id,
        },
        "policy_snapshot": proposal.policy_snapshot,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_aware(value: datetime, code: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(code, "timestamp must be timezone-aware")


def _validate_message_fields(
    *,
    message_id: str,
    channel_id: str,
    created_at: datetime,
    expires_at: datetime | None,
    correlation_id: str | None,
    causation_id: str | None,
    reply_to: str | None,
) -> None:
    for field_name, value in {
        "message_id": message_id,
        "channel_id": channel_id,
    }.items():
        if not value.strip():
            raise ContractError(
                f"interaction.{field_name}_empty",
                f"{field_name} must not be empty",
            )
    _require_aware(created_at, "interaction.created_at_naive")
    if expires_at is not None:
        _require_aware(expires_at, "interaction.expires_at_naive")
        if expires_at <= created_at:
            raise ContractError(
                "interaction.expiry_invalid",
                "message expiry must be after creation time",
            )
    for field_name, value in {
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "reply_to": reply_to,
    }.items():
        if value is not None and not value.strip():
            raise ContractError(
                f"interaction.{field_name}_empty",
                f"{field_name} must not be whitespace when provided",
            )
