from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from misaka_interaction_contracts import (
    DecisionFact,
    DecisionProposal,
    DecisionRef,
    DecisionStatus,
    InteractionMessage,
    MessageCursor,
    MessageDeliveryStatus,
    MessageType,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_kernel_contracts import ContractError


def _principal(principal_id: str = "caller") -> PrincipalRef:
    return PrincipalRef(principal_id, PrincipalKind.APPLICATION)


def test_message_contract_preserves_addressing_and_delivery_facts() -> None:
    scope = ScopeRef("scope-1")
    message = InteractionMessage(
        message_id="message-1",
        channel_id="channel-1",
        sender=_principal(),
        recipient=PrincipalRef("worker", PrincipalKind.AGENT),
        message_type=MessageType.INSTRUCTION,
        payload={"text": "inspect"},
        sequence=1,
        scope=scope,
        correlation_id="corr-1",
        delivery_status=MessageDeliveryStatus.ACCEPTED,
    )

    assert message.scope == scope
    assert message.delivery_status is MessageDeliveryStatus.ACCEPTED
    assert MessageCursor("channel-1", next_sequence=2).next_sequence == 2


def test_message_expiry_must_be_after_creation() -> None:
    created = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    with pytest.raises(ContractError) as raised:
        InteractionMessage(
            message_id="message-1",
            channel_id="channel-1",
            sender=_principal(),
            message_type=MessageType.QUESTION,
            payload={},
            sequence=1,
            scope=ScopeRef("scope-1"),
            created_at=created,
            expires_at=created - timedelta(seconds=1),
        )

    assert raised.value.code == "interaction.expiry_invalid"


def test_decision_rejection_requires_a_reason() -> None:
    ref = DecisionRef("proposal-1", revision=1)
    proposal = DecisionProposal(
        ref=ref,
        plan_hash="a" * 64,
        requested_effects=("workspace_write",),
        scope=ScopeRef("scope-1"),
        created_by=_principal(),
    )

    with pytest.raises(ContractError) as raised:
        DecisionFact(
            ref=proposal.ref,
            status=DecisionStatus.REJECTED,
            decided_by=PrincipalRef("approver", PrincipalKind.HUMAN),
        )

    assert raised.value.code == "decision.rejection_reason_empty"


def test_pending_decision_is_not_a_decision_fact() -> None:
    with pytest.raises(ContractError) as raised:
        DecisionFact(
            ref=DecisionRef("proposal-1", revision=1),
            status=DecisionStatus.PENDING,
            decided_by=_principal(),
        )

    assert raised.value.code == "decision.fact_pending"
