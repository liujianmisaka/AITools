from __future__ import annotations

import os
import uuid

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID
from misaka_coordinator_temporal import (
    TemporalCoordinator,
    TemporalInvocationInput,
    build_temporal_worker,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_contracts import CompletionBoundary, InvocationRequest, InvocationStatus
from misaka_invocation_runtime import InvocationRuntime
from temporalio.client import Client


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
            handle = await coordinator.start(
                f"workflow-{suffix}",
                TemporalInvocationInput.from_request(
                    _request(f"inv-{suffix}"),
                    provider_id="fake",
                    heartbeat_timeout_seconds=10,
                    heartbeat_interval_seconds=1,
                ),
            )
            result = await handle.wait()
            assert result.status is InvocationStatus.SUCCEEDED
            assert result.output == {"answer": "temporal-ok"}
            assert provider.starts == 1
    finally:
        await runtime.stop()
