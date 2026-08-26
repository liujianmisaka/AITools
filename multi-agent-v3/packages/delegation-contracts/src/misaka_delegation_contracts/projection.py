from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from misaka_delegation_contracts.contracts import (
    DelegationMode,
    DelegationSnapshot,
    DelegationStatus,
)


class ConversationState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ActivationState(StrEnum):
    IDLE = "idle"
    PREPARING = "preparing"
    ACTIVE = "active"
    PAUSED = "paused"
    WAITING_INPUT = "waiting_input"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class ConversationProjection:
    delegation_id: str
    session_id: str | None
    conversation_state: ConversationState
    activation_state: ActivationState
    current_activation_id: str | None
    activation_count: int


def project_conversation(
    snapshot: DelegationSnapshot,
    *,
    channel_closed: bool = False,
) -> ConversationProjection:
    if snapshot.status is DelegationStatus.RECONCILIATION_REQUIRED:
        conversation_state = ConversationState.RECONCILIATION_REQUIRED
    elif channel_closed or (
        snapshot.request.mode is DelegationMode.ONE_SHOT and snapshot.report is not None
    ):
        conversation_state = ConversationState.CLOSED
    else:
        conversation_state = ConversationState.OPEN

    activation_state = {
        DelegationStatus.PREPARING: ActivationState.PREPARING,
        DelegationStatus.ACTIVE: ActivationState.ACTIVE,
        DelegationStatus.PAUSED: ActivationState.PAUSED,
        DelegationStatus.WAITING_INPUT: ActivationState.WAITING_INPUT,
    }.get(snapshot.status)
    if activation_state is None:
        activation_state = (
            ActivationState.TERMINAL if snapshot.report is not None else ActivationState.IDLE
        )

    return ConversationProjection(
        delegation_id=snapshot.ref.delegation_id,
        session_id=snapshot.ref.session_id,
        conversation_state=conversation_state,
        activation_state=activation_state,
        current_activation_id=snapshot.current_activation_id,
        activation_count=snapshot.activation_count,
    )
