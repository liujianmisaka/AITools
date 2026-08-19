from __future__ import annotations

import pytest
from misaka_a2a_agent_host import A2AAgentHostConfig, create_fake_a2a_agent_host
from misaka_a2a_capability import TaskRequest, TaskStatus
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_fake_agent import FakeAgentScenario
from misaka_invocation_contracts import CapabilityFeature
from misaka_kernel import HostStatus


def _request(task_id: str) -> TaskRequest:
    return TaskRequest(
        task_id=task_id,
        context_id="context-client",
        message_id=f"message-{task_id}",
        idempotency_key=f"idem-{task_id}",
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "Return the deterministic fake answer"},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        required_features=frozenset({CapabilityFeature.STREAMING}),
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )


def test_wildcard_bind_requires_public_url() -> None:
    with pytest.raises(ValueError, match="public_url"):
        A2AAgentHostConfig(host="0.0.0.0")


@pytest.mark.asyncio
async def test_a2a_agent_host_composes_standalone_agent_host() -> None:
    node = create_fake_a2a_agent_host(FakeAgentScenario(output={"answer": "profile-ok"}))

    await node.start()
    result = await (await node.server.submit(_request("task-composed"))).wait()
    await node.stop()

    assert result.status is TaskStatus.COMPLETED
    assert result.output == {"answer": "profile-ok"}
    assert result.delegation_id is not None
    assert result.invocation_id is not None
    assert result.activation_id is not None
    assert result.delegation_id != result.invocation_id
    assert result.activation_id not in {result.delegation_id, result.invocation_id}
    assert node.host_status is HostStatus.STOPPED
    assert node.server.active_task_count == 0


@pytest.mark.asyncio
async def test_a2a_agent_host_stop_cancels_active_task() -> None:
    node = create_fake_a2a_agent_host(
        FakeAgentScenario(output={"answer": "late"}, delay_seconds=0.1)
    )
    await node.start()
    handle = await node.server.submit(_request("task-stop"))

    await node.stop()
    result = await handle.wait()

    assert result.status is TaskStatus.CANCELLED
    assert node.host_status is HostStatus.STOPPED
