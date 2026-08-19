from __future__ import annotations

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario, FakeFailure
from misaka_invocation_contracts import (
    CapabilityFeature,
    CompletionBoundary,
    InvocationRequest,
    InvocationStatus,
)
from misaka_invocation_runtime import InvocationRuntime
from misaka_kernel_contracts import JsonObject

OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}


def _request(
    invocation_id: str,
    *,
    schema: JsonObject | None = OUTPUT_SCHEMA,
) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "Return a deterministic answer"},
        idempotency_key=f"key-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        required_features=frozenset({CapabilityFeature.STRUCTURED_OUTPUT}),
        output_schema=schema,
    )


@pytest.mark.asyncio
async def test_fake_agent_executes_without_workflow_or_control_plane() -> None:
    provider = FakeAgentProvider(
        FakeAgentScenario(
            output={"answer": "ok"},
            events=({"phase": "reasoning"}, {"phase": "final"}),
        )
    )
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)

    handle = await runtime.submit(_request("inv-1"), provider_id="fake-agent")
    result = await handle.wait()
    snapshot = await handle.snapshot()

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output == {"answer": "ok"}
    assert [event.payload["phase"] for event in snapshot.events if "phase" in event.payload] == [
        "reasoning",
        "final",
    ]
    await runtime.stop()


@pytest.mark.asyncio
async def test_fake_agent_rejects_invalid_structured_output() -> None:
    provider = FakeAgentProvider(FakeAgentScenario(output={"unexpected": True}))
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)

    result = await (await runtime.submit(_request("inv-invalid"), provider_id="fake-agent")).wait()

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "agent.output_contract_violated"
    await runtime.stop()


@pytest.mark.asyncio
async def test_fake_agent_cancel_reaches_terminal_state() -> None:
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "late"}, delay_seconds=0.05))
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)
    handle = await runtime.submit(_request("inv-cancel"), provider_id="fake-agent")
    await provider.started.wait()

    await handle.cancel("user cancelled fake invocation")
    result = await handle.wait()

    assert result.status is InvocationStatus.CANCELLED
    await runtime.stop()


@pytest.mark.asyncio
async def test_fake_agent_can_inject_uncertain_start_failure() -> None:
    provider = FakeAgentProvider(
        FakeAgentScenario(
            failure=FakeFailure(
                "agent.fake_start_unknown",
                "fake provider start outcome is unknown",
                reconciliation_required=True,
            )
        )
    )
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)

    result = await (
        await runtime.submit(_request("inv-uncertain"), provider_id="fake-agent")
    ).wait()

    assert result.status is InvocationStatus.RECONCILIATION_REQUIRED
    assert result.error_code == "agent.fake_start_unknown"
    await runtime.stop()
