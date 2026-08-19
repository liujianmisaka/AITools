from __future__ import annotations

from misaka_interaction_contracts import MessageDeliveryStatus

from misaka_interaction_capability.errors import DeliveryConflict

ALLOWED_DELIVERY_TRANSITIONS: dict[MessageDeliveryStatus, frozenset[MessageDeliveryStatus]] = {
    MessageDeliveryStatus.ACCEPTED: frozenset(
        {
            MessageDeliveryStatus.DELIVERED,
            MessageDeliveryStatus.REJECTED,
            MessageDeliveryStatus.EXPIRED,
        }
    ),
    MessageDeliveryStatus.DELIVERED: frozenset(
        {
            MessageDeliveryStatus.PROCESSED,
            MessageDeliveryStatus.REJECTED,
            MessageDeliveryStatus.EXPIRED,
        }
    ),
    MessageDeliveryStatus.PROCESSED: frozenset({MessageDeliveryStatus.COMPLETED}),
    MessageDeliveryStatus.COMPLETED: frozenset(),
    MessageDeliveryStatus.REJECTED: frozenset(),
    MessageDeliveryStatus.EXPIRED: frozenset(),
}


def validate_delivery_transition(
    current: MessageDeliveryStatus,
    target: MessageDeliveryStatus,
    *,
    expected: MessageDeliveryStatus | None = None,
) -> None:
    if expected is not None and current is not expected:
        raise DeliveryConflict(
            "interaction.delivery_expected_mismatch",
            f"message is {current.value}, expected {expected.value}",
        )
    if target is current:
        return
    if target not in ALLOWED_DELIVERY_TRANSITIONS[current]:
        raise DeliveryConflict(
            "interaction.delivery_transition_invalid",
            f"message cannot transition from {current.value} to {target.value}",
        )
