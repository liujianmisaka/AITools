from __future__ import annotations

import os
import uuid
from dataclasses import replace
from typing import Any, cast

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID
from misaka_coordinator_runtime import ExecutionStatus, ReconciliationState
from misaka_coordinator_temporal import (
    TemporalCoordinator,
    TemporalExecutionHandle,
    TemporalExecutionPlan,
    TemporalInvocationInput,
    TemporalResultPayload,
    build_temporal_worker,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
)
from misaka_invocation_runtime import InvocationRuntime
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import CancelledError as TemporalCancelledError


def _request(invocation_id: str) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation="invoke",
        input={"prompt": "hello"},
        idempotency_key=invocation_id,
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        model="fake/model",
        effort="high",
    )


def test_temporal_input_requires_bounded_heartbeat_configuration() -> None:
    with pytest.raises(ValueError, match="shorter"):
        TemporalInvocationInput(
            "inv-invalid",
            AGENT_CAPABILITY_ID,
            "invoke",
            {"prompt": "hello"},
            "inv-invalid",
            "operation_terminal",
            heartbeat_timeout_seconds=5,
            heartbeat_interval_seconds=5,
        )


def test_temporal_plan_fingerprint_ignores_delivery_attempt() -> None:
    input_value = TemporalInvocationInput.from_request(_request("inv-fingerprint"))
    attempt_two = replace(input_value, attempt=2)
    client = cast(Client, object())
    first = TemporalExecutionPlan(client, "queue-a", input_value, workflow_id="workflow-a")
    second = TemporalExecutionPlan(client, "queue-a", attempt_two, workflow_id="workflow-a")
    different_queue = TemporalExecutionPlan(
        client,
        "queue-b",
        input_value,
        workflow_id="workflow-a",
    )

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != different_queue.fingerprint


class _WorkflowHandleStub:
    id = "workflow-result"
    result_run_id = "run-result"

    def __init__(
        self,
        payload: TemporalResultPayload | None = None,
        *,
        status: str = "COMPLETED",
        failure: BaseException | None = None,
    ) -> None:
        self._payload = payload
        self._status = status
        self._failure = failure

    async def result(self) -> TemporalResultPayload:
        if self._failure is not None:
            raise self._failure
        if self._payload is None:
            raise AssertionError("test workflow payload was not configured")
        return self._payload

    async def cancel(self) -> None:
        return None

    async def describe(self) -> Any:
        return type("Description", (), {"status": self._status})()


class _ClientStub:
    def __init__(self) -> None:
        self.workflow_ids: list[str] = []

    async def start_workflow(self, *args: Any, **kwargs: Any) -> _WorkflowHandleStub:
        del args
        workflow_id = kwargs["id"]
        self.workflow_ids.append(workflow_id)
        return _WorkflowHandleStub(
            TemporalResultPayload(
                invocation_id=workflow_id,
                status=InvocationStatus.SUCCEEDED.value,
            )
        )


@pytest.mark.asyncio
async def test_temporal_result_maps_terminal_invocation_statuses() -> None:
    payload = TemporalResultPayload.from_result(
        InvocationResult(
            invocation_id="inv-result",
            status=InvocationStatus.REJECTED,
            error_code="policy.denied",
            error_message="policy rejected the invocation",
        )
    )
    handle = TemporalExecutionHandle(
        cast(Any, _WorkflowHandleStub(payload)),
        execution_id="inv-result",
    )
    result = await handle.wait()

    assert result.execution_id == "inv-result"
    assert result.activation_id == "inv-result:activation:1"
    assert result.status is ExecutionStatus.FAILED
    assert result.error_code == "policy.denied"


@pytest.mark.asyncio
async def test_temporal_plan_attempt_uses_distinct_execution_and_workflow_identity() -> None:
    client_stub = _ClientStub()
    input_value = TemporalInvocationInput.from_request(_request("inv-attempt"))
    plan = TemporalExecutionPlan(
        cast(Client, client_stub),
        "queue-a",
        input_value,
        workflow_id="workflow-attempt",
    )

    first = await plan.start()
    second = await plan.start(attempt=2)

    assert first.execution_id == "inv-attempt"
    assert second.execution_id == "inv-attempt:attempt:2"
    assert client_stub.workflow_ids == ["workflow-attempt", "workflow-attempt:attempt:2"]


@pytest.mark.asyncio
async def test_temporal_handle_normalizes_workflow_failure_and_reconcile_status() -> None:
    failure = WorkflowFailureError(cause=TemporalCancelledError("cancelled by user"))
    handle = TemporalExecutionHandle(
        cast(Any, _WorkflowHandleStub(failure=failure, status="TIMED_OUT")),
        execution_id="inv-failure",
    )

    result = await handle.wait()
    reconciliation = await handle.reconcile()

    assert result.status is ExecutionStatus.CANCELLED
    assert result.error_code == "temporal.cancelled"
    assert reconciliation.state is ReconciliationState.FAILED


@pytest.mark.asyncio
async def test_temporal_coordinator_integration() -> None:
    target = os.environ.get("MULTI_AGENT_V3_TEMPORAL_TARGET")
    if target is None:
        pytest.skip("MULTI_AGENT_V3_TEMPORAL_TARGET is not configured")
    suffix = uuid.uuid4().hex
    task_queue = f"multi-agent-v3-{suffix}"
    runtime = InvocationRuntime(cancellation_timeout_seconds=1, shutdown_timeout_seconds=1)
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "temporal-ok"}))
    await runtime.register_provider("fake", provider)
    client = await Client.connect(target)
    worker = build_temporal_worker(client, runtime, task_queue=task_queue)
    coordinator = TemporalCoordinator(client, task_queue=task_queue)
    try:
        async with worker:
            handle = await coordinator.submit(
                TemporalExecutionPlan(
                    client,
                    task_queue,
                    TemporalInvocationInput.from_request(
                        _request(f"inv-{suffix}"),
                        provider_id="fake",
                        heartbeat_timeout_seconds=10,
                        heartbeat_interval_seconds=1,
                    ),
                )
            )
            result = await handle.wait()
            assert result.status is ExecutionStatus.SUCCEEDED
            assert result.output == {"answer": "temporal-ok"}
            assert provider.starts == 1
    finally:
        await runtime.stop()
