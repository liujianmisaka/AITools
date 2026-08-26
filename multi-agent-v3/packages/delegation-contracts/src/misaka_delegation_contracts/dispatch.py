from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from misaka_interaction_contracts import MessageType, PrincipalRef
from misaka_kernel_contracts import ContractError, JsonObject


class MessageDispatchMode(StrEnum):
    APPEND = "append"
    INTERRUPT_CONTINUE = "interrupt_continue"


class MessageDispatchStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    COMPLETED = "completed"
    REJECTED = "rejected"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class MessageDispatchStrategy(StrEnum):
    STEERED_CURRENT_ACTIVATION = "steered_current_activation"
    QUEUED_FOR_NEXT_ACTIVATION = "queued_for_next_activation"
    STARTED_NEW_ACTIVATION = "started_new_activation"
    INTERRUPTED_AND_CONTINUED = "interrupted_and_continued"
    REPLIED_TO_LIVE_QUESTION = "replied_to_live_question"
    REPLIED_WITH_NEW_ACTIVATION = "replied_with_new_activation"


@dataclass(frozen=True, slots=True)
class MessageDispatchRequest:
    dispatch_id: str
    delegation_id: str
    idempotency_key: str
    message_id: str
    actor: PrincipalRef
    session_id: str
    expected_activation_id: str | None
    delivery: MessageDispatchMode
    message_type: MessageType
    payload: JsonObject
    recipient: PrincipalRef | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    reply_to: str | None = None
    model: str | None = None
    effort: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for field_name, value in {
            "dispatch_id": self.dispatch_id,
            "delegation_id": self.delegation_id,
            "idempotency_key": self.idempotency_key,
            "message_id": self.message_id,
            "session_id": self.session_id,
        }.items():
            if not value.strip():
                raise ContractError(
                    f"message_dispatch.{field_name}_empty",
                    f"{field_name} must not be empty",
                )
        if self.expected_activation_id is not None and not self.expected_activation_id.strip():
            raise ContractError(
                "message_dispatch.expected_activation_id_empty",
                "expected_activation_id must not be whitespace when provided",
            )
        for field_name, value in {
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "reply_to": self.reply_to,
            "model": self.model,
            "effort": self.effort,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"message_dispatch.{field_name}_empty",
                    f"{field_name} must not be whitespace when provided",
                )
        if (self.model is None) != (self.effort is None):
            raise ContractError(
                "message_dispatch.execution_selection_incomplete",
                "model and effort must be provided together",
            )
        if self.message_type is MessageType.ANSWER:
            if self.reply_to is None or self.correlation_id is None:
                raise ContractError(
                    "message_dispatch.answer_reference_missing",
                    "answer dispatches require reply_to and correlation_id",
                )
        elif self.reply_to is not None:
            raise ContractError(
                "message_dispatch.reply_type_invalid",
                "reply_to is only valid for answer dispatches",
            )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ContractError(
                "message_dispatch.created_at_naive",
                "created_at must be timezone-aware",
            )


@dataclass(frozen=True, slots=True)
class MessageDispatchTransition:
    status: MessageDispatchStatus
    expected_status: MessageDispatchStatus
    applied_strategy: MessageDispatchStrategy | None = None
    previous_activation_id: str | None = None
    current_activation_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for field_name, value in {
            "previous_activation_id": self.previous_activation_id,
            "current_activation_id": self.current_activation_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"message_dispatch.{field_name}_empty",
                    f"{field_name} must not be whitespace when provided",
                )
        _validate_status_details(
            status=self.status,
            applied_strategy=self.applied_strategy,
            error_code=self.error_code,
            error_message=self.error_message,
        )
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ContractError(
                "message_dispatch.occurred_at_naive",
                "occurred_at must be timezone-aware",
            )


@dataclass(frozen=True, slots=True)
class MessageDispatchSnapshot:
    request: MessageDispatchRequest
    status: MessageDispatchStatus = MessageDispatchStatus.ACCEPTED
    revision: int = 1
    applied_strategy: MessageDispatchStrategy | None = None
    previous_activation_id: str | None = None
    current_activation_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ContractError(
                "message_dispatch.revision_invalid",
                "dispatch revision must be at least one",
            )
        _validate_status_details(
            status=self.status,
            applied_strategy=self.applied_strategy,
            error_code=self.error_code,
            error_message=self.error_message,
        )
        for field_name, value in {
            "previous_activation_id": self.previous_activation_id,
            "current_activation_id": self.current_activation_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }.items():
            if value is not None and not value.strip():
                raise ContractError(
                    f"message_dispatch.{field_name}_empty",
                    f"{field_name} must not be whitespace when provided",
                )
        if self.updated_at is not None:
            if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
                raise ContractError(
                    "message_dispatch.updated_at_naive",
                    "updated_at must be timezone-aware",
                )
            if self.updated_at < self.request.created_at:
                raise ContractError(
                    "message_dispatch.updated_at_invalid",
                    "updated_at must not precede created_at",
                )


def _validate_status_details(
    *,
    status: MessageDispatchStatus,
    applied_strategy: MessageDispatchStrategy | None,
    error_code: str | None,
    error_message: str | None,
) -> None:
    if status in {
        MessageDispatchStatus.REJECTED,
        MessageDispatchStatus.RECONCILIATION_REQUIRED,
    }:
        if error_code is None or error_message is None:
            raise ContractError(
                "message_dispatch.error_required",
                f"{status.value} dispatches require error details",
            )
        if applied_strategy is not None:
            raise ContractError(
                "message_dispatch.error_strategy_invalid",
                "failed dispatches cannot declare an applied strategy",
            )
        return
    if error_code is not None or error_message is not None:
        raise ContractError(
            "message_dispatch.error_invalid",
            f"{status.value} dispatches cannot define error details",
        )
    if status is MessageDispatchStatus.QUEUED:
        if applied_strategy is not MessageDispatchStrategy.QUEUED_FOR_NEXT_ACTIVATION:
            raise ContractError(
                "message_dispatch.queue_strategy_required",
                "queued dispatches must declare queued_for_next_activation",
            )
    elif status is MessageDispatchStatus.COMPLETED:
        if applied_strategy is None:
            raise ContractError(
                "message_dispatch.strategy_required",
                "completed dispatches require an applied strategy",
            )
    elif applied_strategy is not None:
        raise ContractError(
            "message_dispatch.strategy_invalid",
            f"{status.value} dispatches cannot declare an applied strategy",
        )
