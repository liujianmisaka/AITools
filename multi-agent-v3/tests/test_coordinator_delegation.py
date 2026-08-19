from __future__ import annotations

from asyncio import wait_for
from dataclasses import replace
from typing import cast

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_agent_host_profile import create_fake_agent_host
from misaka_coordinator_adapters import DelegationExecutionHandle, DelegationExecutionPlan
from misaka_coordinator_runtime import ExecutionEvent, ExecutionStatus, ReconciliationState
from misaka_delegation_capability import DelegationRuntimePort
from misaka_delegation_contracts import DelegationMode, DelegationRequest
from misaka_delegation_runtime import DelegationRuntime
from misaka_fake_agent import FakeAgentScenario
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
from misaka_interaction_memory import MemoryInteractionChannelStore


def _request(delegation_id: str) -> DelegationRequest:
    principal = PrincipalRef("parent", PrincipalKind.APPLICATION)
    return DelegationRequest(
        delegation_id=delegation_id,
        idempotency_key=f"idem-{delegation_id}",
        initiator=principal,
        controller=principal,
        scope=ScopeRef("scope-1"),
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "return the fake answer"},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        mode=DelegationMode.ONE_SHOT,
    )


@pytest.mark.asyncio
async def test_delegation_execution_plan_maps_report_events_and_reconcile() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    try:
        plan = DelegationExecutionPlan(runtime, _request("delegation-execution"))
        handle = await plan.start()
        result = await handle.wait()
        events = [event async for event in handle.events()]
        reconciliation = await handle.reconcile()

        assert handle.execution_id == "delegation-execution"
        assert handle.activation_id == "delegation-execution:activation:1"
        assert result.status is ExecutionStatus.SUCCEEDED
        assert result.output == {"answer": "ok"}
        assert result.metadata["source_invocation_id"] == ("delegation-execution:invocation:1")
        assert events[-1].status is ExecutionStatus.SUCCEEDED
        assert reconciliation.state is ReconciliationState.SUCCEEDED
    finally:
        await runtime.stop()
        await host.stop()


def test_delegation_execution_plan_fingerprint_is_request_based() -> None:
    request = _request("delegation-fingerprint")
    runtime = cast(DelegationRuntimePort, object())
    first = DelegationExecutionPlan(runtime, request)
    second = DelegationExecutionPlan(runtime, request)

    assert first.execution_id == request.delegation_id
    assert first.fingerprint == second.fingerprint


@pytest.mark.asyncio
async def test_delegation_execution_plan_rejects_automatic_retry_attempts() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    try:
        plan = DelegationExecutionPlan(runtime, _request("delegation-retry"))
        with pytest.raises(ValueError, match="new delegation identity"):
            await plan.start(attempt=2)
    finally:
        await runtime.stop()
        await host.stop()


@pytest.mark.asyncio
async def test_continuable_delegation_events_end_at_the_current_activation() -> None:
    host = create_fake_agent_host(FakeAgentScenario(output={"answer": "ok"}))
    runtime = DelegationRuntime(host.runtime, MemoryInteractionChannelStore())
    await host.start()
    try:
        request = _request("delegation-continuable-execution")
        request = replace(request, mode=DelegationMode.CONTINUABLE)
        handle = await DelegationExecutionPlan(runtime, request).start()
        events = await wait_for(_collect_events(handle), timeout=1)

        assert events
        assert events[-1].status is ExecutionStatus.SUCCEEDED
    finally:
        await runtime.stop()
        await host.stop()


async def _collect_events(handle: DelegationExecutionHandle) -> list[ExecutionEvent]:
    return [event async for event in handle.events()]
