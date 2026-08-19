from datetime import UTC

import pytest
from misaka_invocation_contracts import (
    CapabilityDescriptor,
    CapabilityFeature,
    CapabilityOperation,
    CompletionBoundary,
    InvocationRequest,
    InvocationStatus,
    request_fingerprint,
)
from misaka_kernel_contracts import (
    ContractError,
    EventMode,
    ModuleId,
    ModuleManifest,
    RuntimeEvent,
    ServiceKey,
)


def test_kernel_manifest_and_event_are_value_objects() -> None:
    manifest = ModuleManifest(
        module_id=ModuleId("test.module"),
        version="0.1.0",
        provides=(),
    )
    event = RuntimeEvent(name="module.started")

    assert manifest.module_id == "test.module"
    assert event.name == "module.started"
    assert event.occurred_at.tzinfo == UTC
    assert EventMode.EMIT.value == "emit"
    assert ServiceKey("example.service") == "example.service"


def test_capability_descriptor_declares_operations_and_features() -> None:
    descriptor = CapabilityDescriptor(
        capability_id="agent.invocation",
        version="1.0.0",
        operations=(CapabilityOperation(name="invoke"),),
        features=frozenset({CapabilityFeature.STREAMING, CapabilityFeature.CANCELLATION}),
    )

    assert descriptor.operations[0].name == "invoke"
    assert CapabilityFeature.STREAMING in descriptor.features


def test_request_fingerprint_excludes_delivery_attempt() -> None:
    first = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        attempt=1,
    )
    retry = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        attempt=2,
    )

    assert request_fingerprint(first) == request_fingerprint(retry)


def test_request_fingerprint_includes_required_features() -> None:
    baseline = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
    )
    streaming = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        required_features=frozenset({CapabilityFeature.STREAMING}),
    )

    assert request_fingerprint(baseline) != request_fingerprint(streaming)


def test_request_fingerprint_includes_output_schema() -> None:
    baseline = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
    )
    structured = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        output_schema={
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        },
    )

    assert request_fingerprint(baseline) != request_fingerprint(structured)


def test_request_fingerprint_includes_policy_context() -> None:
    baseline = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
    )
    constrained = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        policy_context={"network_policy": "deny"},
    )

    assert request_fingerprint(baseline) != request_fingerprint(constrained)


def test_request_fingerprint_includes_model_and_effort() -> None:
    baseline = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        model="pixel/gpt-5.6-luna",
        effort="high",
    )
    changed_model = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        model="sensenova/deepseek-v4-flash",
        effort="high",
    )
    changed_effort = InvocationRequest(
        invocation_id="inv-1",
        capability_id="agent.invocation",
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key="key-1",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        model="pixel/gpt-5.6-luna",
        effort="medium",
    )

    assert request_fingerprint(baseline) != request_fingerprint(changed_model)
    assert request_fingerprint(baseline) != request_fingerprint(changed_effort)


def test_invocation_terminal_statuses_are_explicit() -> None:
    assert InvocationStatus.SUCCEEDED.value == "succeeded"
    assert InvocationStatus.RECONCILIATION_REQUIRED.value == "reconciliation_required"


def test_contracts_reject_empty_identity_and_non_terminal_result() -> None:
    try:
        InvocationRequest(
            invocation_id="",
            capability_id="agent.invocation",
            operation="invoke",
            input={},
            idempotency_key="key-1",
            completion_boundary=CompletionBoundary.ACCEPTED,
        )
    except ContractError as exc:
        assert exc.code == "invocation.invocation_id_empty"
    else:
        raise AssertionError("empty invocation id must be rejected")

    try:
        from misaka_invocation_contracts import InvocationResult

        InvocationResult(invocation_id="inv-1", status=InvocationStatus.RUNNING)
    except ContractError as exc:
        assert exc.code == "result.status_non_terminal"
    else:
        raise AssertionError("non-terminal result status must be rejected")


def test_capability_operation_names_are_unique() -> None:
    try:
        CapabilityDescriptor(
            capability_id="agent.invocation",
            version="1.0.0",
            operations=(CapabilityOperation(name="invoke"), CapabilityOperation(name="invoke")),
        )
    except ContractError as exc:
        assert exc.code == "capability.operation_duplicate"
    else:
        raise AssertionError("duplicate operation names must be rejected")


def test_capability_requires_at_least_one_operation() -> None:
    with pytest.raises(ContractError) as raised:
        CapabilityDescriptor(
            capability_id="agent.invocation",
            version="1.0.0",
            operations=(),
        )

    assert raised.value.code == "capability.operations_empty"
