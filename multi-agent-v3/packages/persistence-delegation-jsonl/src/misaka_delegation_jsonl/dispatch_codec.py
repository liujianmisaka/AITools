from __future__ import annotations

from datetime import datetime
from typing import cast

from misaka_delegation_contracts.dispatch import (
    MessageDispatchMode,
    MessageDispatchRequest,
    MessageDispatchStatus,
    MessageDispatchStrategy,
    MessageDispatchTransition,
)
from misaka_interaction_contracts import MessageType, PrincipalKind, PrincipalRef
from misaka_kernel_contracts import JsonObject


def encode_dispatch_request(request: MessageDispatchRequest) -> JsonObject:
    return {
        "dispatch_id": request.dispatch_id,
        "delegation_id": request.delegation_id,
        "idempotency_key": request.idempotency_key,
        "message_id": request.message_id,
        "actor": _encode_principal(request.actor),
        "session_id": request.session_id,
        "expected_activation_id": request.expected_activation_id,
        "delivery": request.delivery.value,
        "message_type": request.message_type.value,
        "payload": request.payload,
        "recipient": (
            _encode_principal(request.recipient) if request.recipient is not None else None
        ),
        "correlation_id": request.correlation_id,
        "causation_id": request.causation_id,
        "reply_to": request.reply_to,
        "model": request.model,
        "effort": request.effort,
        "created_at": request.created_at.isoformat(),
    }


def decode_dispatch_request(payload: JsonObject) -> MessageDispatchRequest:
    recipient = payload.get("recipient")
    return MessageDispatchRequest(
        dispatch_id=_required_string(payload, "dispatch_id"),
        delegation_id=_required_string(payload, "delegation_id"),
        idempotency_key=_required_string(payload, "idempotency_key"),
        message_id=_required_string(payload, "message_id"),
        actor=_decode_principal(_required_object(payload, "actor")),
        session_id=_required_string(payload, "session_id"),
        expected_activation_id=_optional_string(payload.get("expected_activation_id")),
        delivery=MessageDispatchMode(_required_string(payload, "delivery")),
        message_type=MessageType(_required_string(payload, "message_type")),
        payload=_required_object(payload, "payload"),
        recipient=(
            _decode_principal(_required_object_value(recipient, "recipient"))
            if recipient is not None
            else None
        ),
        correlation_id=_optional_string(payload.get("correlation_id")),
        causation_id=_optional_string(payload.get("causation_id")),
        reply_to=_optional_string(payload.get("reply_to")),
        model=_optional_string(payload.get("model")),
        effort=_optional_string(payload.get("effort")),
        created_at=datetime.fromisoformat(_required_string(payload, "created_at")),
    )


def encode_dispatch_transition(transition: MessageDispatchTransition) -> JsonObject:
    return {
        "status": transition.status.value,
        "expected_status": transition.expected_status.value,
        "applied_strategy": (
            transition.applied_strategy.value if transition.applied_strategy is not None else None
        ),
        "previous_activation_id": transition.previous_activation_id,
        "current_activation_id": transition.current_activation_id,
        "error_code": transition.error_code,
        "error_message": transition.error_message,
        "occurred_at": transition.occurred_at.isoformat(),
    }


def decode_dispatch_transition(payload: JsonObject) -> MessageDispatchTransition:
    raw_strategy = _optional_string(payload.get("applied_strategy"))
    return MessageDispatchTransition(
        status=MessageDispatchStatus(_required_string(payload, "status")),
        expected_status=MessageDispatchStatus(_required_string(payload, "expected_status")),
        applied_strategy=(
            MessageDispatchStrategy(raw_strategy) if raw_strategy is not None else None
        ),
        previous_activation_id=_optional_string(payload.get("previous_activation_id")),
        current_activation_id=_optional_string(payload.get("current_activation_id")),
        error_code=_optional_string(payload.get("error_code")),
        error_message=_optional_string(payload.get("error_message")),
        occurred_at=datetime.fromisoformat(_required_string(payload, "occurred_at")),
    )


def _encode_principal(principal: PrincipalRef) -> JsonObject:
    return {
        "principal_id": principal.principal_id,
        "kind": principal.kind.value,
        "display_name": principal.display_name,
    }


def _decode_principal(payload: JsonObject) -> PrincipalRef:
    return PrincipalRef(
        principal_id=_required_string(payload, "principal_id"),
        kind=PrincipalKind(_required_string(payload, "kind")),
        display_name=_optional_string(payload.get("display_name")) or "",
    )


def _required_object(payload: JsonObject, name: str) -> JsonObject:
    return _required_object_value(payload.get(name), name)


def _required_object_value(value: object, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(JsonObject, value)


def _required_string(payload: JsonObject, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional string field has an invalid value")
    return value
