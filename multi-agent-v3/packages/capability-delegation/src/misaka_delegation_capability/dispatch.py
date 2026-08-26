from __future__ import annotations

from misaka_delegation_contracts.dispatch import (
    MessageDispatchStatus,
    MessageDispatchTransition,
)

from misaka_delegation_capability.errors import DispatchConflict

ALLOWED_DISPATCH_TRANSITIONS: dict[MessageDispatchStatus, frozenset[MessageDispatchStatus]] = {
    MessageDispatchStatus.ACCEPTED: frozenset(
        {
            MessageDispatchStatus.QUEUED,
            MessageDispatchStatus.DISPATCHING,
            MessageDispatchStatus.REJECTED,
        }
    ),
    MessageDispatchStatus.QUEUED: frozenset(
        {
            MessageDispatchStatus.DISPATCHING,
            MessageDispatchStatus.REJECTED,
        }
    ),
    MessageDispatchStatus.DISPATCHING: frozenset(
        {
            MessageDispatchStatus.COMPLETED,
            MessageDispatchStatus.REJECTED,
            MessageDispatchStatus.RECONCILIATION_REQUIRED,
        }
    ),
    MessageDispatchStatus.COMPLETED: frozenset(),
    MessageDispatchStatus.REJECTED: frozenset(),
    MessageDispatchStatus.RECONCILIATION_REQUIRED: frozenset(),
}


def validate_dispatch_transition(
    current: MessageDispatchStatus,
    transition: MessageDispatchTransition,
) -> None:
    if current is not transition.expected_status:
        raise DispatchConflict(
            "message_dispatch.expected_status_mismatch",
            f"dispatch is {current.value}, expected {transition.expected_status.value}",
        )
    if transition.status is current:
        return
    if transition.status not in ALLOWED_DISPATCH_TRANSITIONS[current]:
        raise DispatchConflict(
            "message_dispatch.transition_invalid",
            f"dispatch cannot transition from {current.value} to {transition.status.value}",
        )
