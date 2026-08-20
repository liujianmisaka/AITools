from __future__ import annotations

import pytest
from misaka_delegation_contracts import (
    ContinuationOperation,
    ContinuationRequest,
    DelegationBudget,
    DelegationIntent,
    DelegationMode,
    DelegationPolicy,
    DelegationRef,
    DelegationReport,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
)
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
from misaka_kernel_contracts import ContractError


def _principal(principal_id: str, kind: PrincipalKind) -> PrincipalRef:
    return PrincipalRef(principal_id, kind)


def _request(*, mode: DelegationMode = DelegationMode.ONE_SHOT) -> DelegationRequest:
    return DelegationRequest(
        delegation_id="delegation-1",
        idempotency_key="key-1",
        initiator=_principal("caller", PrincipalKind.APPLICATION),
        controller=_principal("caller", PrincipalKind.APPLICATION),
        scope=ScopeRef("scope-1"),
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "inspect"},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        output_schema={"type": "object"},
        mode=mode,
    )


def test_continuable_delegation_can_be_allocated_before_session_binding() -> None:
    request = _request(mode=DelegationMode.CONTINUABLE)

    assert request.session_id is None
    assert request.channel_id is None


def test_follow_up_requires_session_and_message_identity() -> None:
    with pytest.raises(ContractError) as raised:
        ContinuationRequest(
            request_id="continuation-1",
            delegation_id="delegation-1",
            operation=ContinuationOperation.FOLLOW_UP,
            actor=_principal("caller", PrincipalKind.APPLICATION),
            idempotency_key="key-2",
        )

    assert raised.value.code == "continuation.message_refs_required"


def test_follow_up_carries_correlation_and_expected_activation() -> None:
    request = ContinuationRequest(
        request_id="continuation-1",
        delegation_id="delegation-1",
        operation=ContinuationOperation.FOLLOW_UP,
        actor=_principal("caller", PrincipalKind.APPLICATION),
        idempotency_key="key-2",
        session_id="session-1",
        message_id="message-2",
        expected_activation_id="activation-1",
        correlation_id="corr-1",
        reply_to="message-1",
        input={"text": "continue"},
    )

    assert request.expected_activation_id == "activation-1"
    assert request.reply_to == "message-1"


def test_reply_is_a_first_class_continuation_operation() -> None:
    request = ContinuationRequest(
        request_id="reply-1",
        delegation_id="delegation-1",
        operation=ContinuationOperation.REPLY,
        actor=_principal("caller", PrincipalKind.APPLICATION),
        idempotency_key="reply-key",
        session_id="session-1",
        message_id="answer-1",
        correlation_id="corr-1",
        reply_to="question-1",
        input={"text": "yes"},
    )

    assert request.operation is ContinuationOperation.REPLY


def test_follow_up_rejects_blank_message_identity() -> None:
    with pytest.raises(ContractError) as raised:
        ContinuationRequest(
            request_id="continuation-1",
            delegation_id="delegation-1",
            operation=ContinuationOperation.FOLLOW_UP,
            actor=_principal("caller", PrincipalKind.APPLICATION),
            idempotency_key="key-2",
            session_id="session-1",
            message_id=" ",
        )

    assert raised.value.code == "continuation.message_id_empty"


def test_snapshot_rejects_duplicate_children() -> None:
    child = DelegationRef("child-1")

    with pytest.raises(ContractError) as raised:
        DelegationSnapshot(
            ref=DelegationRef("delegation-1"),
            request=_request(),
            status=DelegationStatus.ACTIVE,
            child_refs=(child, child),
        )

    assert raised.value.code == "delegation.child_duplicate"


def test_snapshot_requires_request_and_ref_identity_to_match() -> None:
    with pytest.raises(ContractError) as raised:
        DelegationSnapshot(
            ref=DelegationRef("parent-2"),
            request=_request(),
            status=DelegationStatus.ACTIVE,
        )

    assert raised.value.code == "delegation.snapshot_id_mismatch"


def test_snapshot_rejects_report_for_another_delegation() -> None:
    with pytest.raises(ContractError) as raised:
        DelegationSnapshot(
            ref=DelegationRef("delegation-1"),
            request=_request(),
            status=DelegationStatus.COMPLETED,
            report=DelegationReport(
                delegation_id="delegation-2",
                status=DelegationStatus.COMPLETED,
            ),
        )

    assert raised.value.code == "delegation.report_id_mismatch"


def test_report_only_accepts_terminal_delegation_status() -> None:
    with pytest.raises(ContractError) as raised:
        DelegationReport(
            delegation_id="delegation-1",
            status=DelegationStatus.ACTIVE,
        )

    assert raised.value.code == "delegation.report_status_non_terminal"


def test_snapshot_tracks_activation_identity_and_count() -> None:
    snapshot = DelegationSnapshot(
        ref=DelegationRef("delegation-1"),
        request=_request(),
        status=DelegationStatus.ACTIVE,
        current_invocation_id="delegation-1:invocation:1",
        current_activation_id="delegation-1:activation:1",
        activation_count=1,
    )

    assert snapshot.current_invocation_id == "delegation-1:invocation:1"
    assert snapshot.current_activation_id == "delegation-1:activation:1"
    assert snapshot.activation_count == 1


def test_snapshot_rejects_report_history_from_another_delegation() -> None:
    with pytest.raises(ContractError) as raised:
        DelegationSnapshot(
            ref=DelegationRef("delegation-1"),
            request=_request(),
            status=DelegationStatus.COMPLETED,
            report_history=(
                DelegationReport(
                    delegation_id="delegation-2",
                    status=DelegationStatus.COMPLETED,
                ),
            ),
        )

    assert raised.value.code == "delegation.report_history_id_mismatch"


def test_policy_rejects_conflicting_tool_filters_and_invalid_budget() -> None:
    with pytest.raises(ContractError) as tool_error:
        DelegationPolicy(
            tool_allowlist=frozenset({"repo.read"}),
            tool_denylist=frozenset({"repo.read"}),
        )
    assert tool_error.value.code == "delegation.tool_policy_conflict"

    with pytest.raises(ContractError) as budget_error:
        DelegationBudget(fan_out_limit=0)
    assert budget_error.value.code == "delegation.budget_limit_invalid"


def test_delegation_ref_rejects_negative_depth() -> None:
    with pytest.raises(ContractError) as raised:
        DelegationRef("delegation-1", depth=-1)
    assert raised.value.code == "delegation.depth_invalid"


def test_snapshot_exposes_an_independent_delegation_intent() -> None:
    request = _request()
    intent = DelegationIntent("intent-1", request)
    snapshot = DelegationSnapshot(
        ref=DelegationRef(request.delegation_id),
        request=request,
        intent=intent,
        status=DelegationStatus.PROPOSED,
    )

    assert snapshot.intent is intent
    assert snapshot.intent is not None
    assert snapshot.intent.delegation_id == request.delegation_id
