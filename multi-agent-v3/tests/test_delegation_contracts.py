from __future__ import annotations

from dataclasses import replace

import pytest
from misaka_delegation_contracts import (
    CONTINUATION_OPERATION_CATALOG,
    ContinuationActivationEffect,
    ContinuationCompletionBoundary,
    ContinuationConcurrencyRule,
    ContinuationLeaseRequirement,
    ContinuationOperation,
    ContinuationOperationCatalog,
    ContinuationRecoveryPolicy,
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
    continuation_operation_catalog,
    continuation_operation_spec,
)
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
from misaka_kernel_contracts import ContractError, JsonObject


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


def test_continuation_operation_catalog_is_complete_and_declares_execution_boundaries() -> None:
    specs = continuation_operation_catalog()

    assert tuple(spec.operation for spec in specs) == tuple(ContinuationOperation)
    assert tuple(CONTINUATION_OPERATION_CATALOG) == tuple(ContinuationOperation)
    assert all(
        operation is spec.operation for operation, spec in CONTINUATION_OPERATION_CATALOG.items()
    )

    follow_up = continuation_operation_spec(ContinuationOperation.FOLLOW_UP)
    assert follow_up.activation_effect is ContinuationActivationEffect.CREATE_NEW
    assert follow_up.lease_requirement is ContinuationLeaseRequirement.SESSION
    assert follow_up.concurrency is ContinuationConcurrencyRule.SESSION_EXCLUSIVE
    assert follow_up.completion_boundary is ContinuationCompletionBoundary.ACTIVATION_TERMINAL
    assert follow_up.recovery_policy is ContinuationRecoveryPolicy.RECONCILE_LIVE
    assert follow_up.requires_expected_activation is True

    reconcile = continuation_operation_spec(ContinuationOperation.RECONCILE)
    assert reconcile.requires_session is True
    assert reconcile.requires_channel is False


def test_continuation_operation_catalog_returns_recursive_schema_copies() -> None:
    first = CONTINUATION_OPERATION_CATALOG[ContinuationOperation.FOLLOW_UP]
    properties = first.output_schema["properties"]
    assert isinstance(properties, dict)
    status_schema = properties["status"]
    assert isinstance(status_schema, dict)
    status_schema["type"] = "integer"
    first.input_schema["additionalProperties"] = False

    second = continuation_operation_spec(ContinuationOperation.FOLLOW_UP)
    second_properties = second.output_schema["properties"]
    assert isinstance(second_properties, dict)
    second_status_schema = second_properties["status"]
    assert isinstance(second_status_schema, dict)
    assert second_status_schema["type"] == "string"
    assert second.input_schema["additionalProperties"] is True

    source_schema: JsonObject = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }
    copied = replace(second, input_schema=source_schema)
    source_properties = source_schema["properties"]
    assert isinstance(source_properties, dict)
    source_value_schema = source_properties["value"]
    assert isinstance(source_value_schema, dict)
    source_value_schema["type"] = "integer"
    copied_properties = copied.input_schema["properties"]
    assert isinstance(copied_properties, dict)
    copied_value_schema = copied_properties["value"]
    assert isinstance(copied_value_schema, dict)
    assert copied_value_schema["type"] == "string"

    source_specs = {
        operation: continuation_operation_spec(operation) for operation in ContinuationOperation
    }
    catalog = ContinuationOperationCatalog(source_specs)
    source_specs[ContinuationOperation.PREPARE].input_schema["additionalProperties"] = False
    assert catalog[ContinuationOperation.PREPARE].input_schema["additionalProperties"] is True


def test_continuation_operation_spec_enforces_reference_hierarchy() -> None:
    session_spec = continuation_operation_spec(ContinuationOperation.PREPARE)
    with pytest.raises(ContractError) as lease_error:
        replace(session_spec, requires_session=False, requires_channel=False)
    assert lease_error.value.code == "continuation.operation_lease_scope_invalid"

    with pytest.raises(ContractError) as channel_error:
        replace(
            session_spec,
            lease_requirement=ContinuationLeaseRequirement.NONE,
            requires_session=False,
        )
    assert channel_error.value.code == "continuation.operation_channel_scope_invalid"

    message_spec = continuation_operation_spec(ContinuationOperation.FOLLOW_UP)
    with pytest.raises(ContractError) as message_error:
        replace(message_spec, requires_channel=False)
    assert message_error.value.code == "continuation.operation_message_scope_invalid"

    with pytest.raises(ContractError) as reply_error:
        replace(message_spec, requires_message=False, requires_reply_target=True)
    assert reply_error.value.code == "continuation.operation_reply_target_invalid"

    with pytest.raises(ContractError) as correlation_error:
        replace(message_spec, requires_message=False, requires_correlation=True)
    assert correlation_error.value.code == "continuation.operation_correlation_scope_invalid"


def test_continuation_operation_catalog_rejects_invalid_construction() -> None:
    prepare = continuation_operation_spec(ContinuationOperation.PREPARE)
    with pytest.raises(ContractError) as key_error:
        ContinuationOperationCatalog(
            {ContinuationOperation.PREPARE: replace(prepare, operation=ContinuationOperation.START)}
        )
    assert key_error.value.code == "continuation.operation_catalog_key_mismatch"

    with pytest.raises(ContractError) as completeness_error:
        ContinuationOperationCatalog({ContinuationOperation.PREPARE: prepare})
    assert completeness_error.value.code == "continuation.operation_catalog_incomplete"


def test_catalog_validates_input_schema_and_activation_fence() -> None:
    with pytest.raises(ContractError) as schema_error:
        ContinuationRequest(
            request_id="start-1",
            delegation_id="delegation-1",
            operation=ContinuationOperation.START,
            actor=_principal("caller", PrincipalKind.APPLICATION),
            idempotency_key="start-key",
            session_id="session-1",
            expected_activation_id="activation-1",
            input={"replacement": True},
        )
    assert schema_error.value.code == "continuation.input_schema_invalid"

    with pytest.raises(ContractError) as fence_error:
        ContinuationRequest(
            request_id="follow-up-1",
            delegation_id="delegation-1",
            operation=ContinuationOperation.FOLLOW_UP,
            actor=_principal("caller", PrincipalKind.APPLICATION),
            idempotency_key="follow-up-key",
            session_id="session-1",
            message_id="message-1",
        )
    assert fence_error.value.code == "continuation.activation_fence_required"

    with pytest.raises(ContractError) as empty_fence_error:
        ContinuationRequest(
            request_id="follow-up-2",
            delegation_id="delegation-1",
            operation=ContinuationOperation.FOLLOW_UP,
            actor=_principal("caller", PrincipalKind.APPLICATION),
            idempotency_key="follow-up-key-2",
            session_id="session-1",
            message_id="message-1",
            expected_activation_id=" ",
        )
    assert empty_fence_error.value.code == "continuation.expected_activation_id_empty"


def test_reply_is_a_first_class_continuation_operation() -> None:
    request = ContinuationRequest(
        request_id="reply-1",
        delegation_id="delegation-1",
        operation=ContinuationOperation.REPLY,
        actor=_principal("caller", PrincipalKind.APPLICATION),
        idempotency_key="reply-key",
        session_id="session-1",
        message_id="answer-1",
        expected_activation_id="activation-1",
        correlation_id="corr-1",
        reply_to="question-1",
        input={"text": "yes"},
    )

    assert request.operation is ContinuationOperation.REPLY


def test_ack_requires_a_target_message_and_control_ops_require_activation_fence() -> None:
    with pytest.raises(ContractError) as ack_error:
        ContinuationRequest(
            request_id="ack-1",
            delegation_id="delegation-1",
            operation=ContinuationOperation.ACK,
            actor=_principal("caller", PrincipalKind.APPLICATION),
            idempotency_key="ack-key",
            session_id="session-1",
            message_id="ack-message",
        )
    assert ack_error.value.code == "continuation.ack_target_required"

    with pytest.raises(ContractError) as pause_error:
        ContinuationRequest(
            request_id="pause-1",
            delegation_id="delegation-1",
            operation=ContinuationOperation.PAUSE,
            actor=_principal("caller", PrincipalKind.APPLICATION),
            idempotency_key="pause-key",
            session_id="session-1",
        )
    assert pause_error.value.code == "continuation.activation_fence_required"


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
