from __future__ import annotations

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_fake_agent import FakeAgentProvider
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationRequest,
    InvocationStatus,
    PolicyDecision,
    PolicyEffect,
)
from misaka_invocation_runtime import InvocationRuntime
from misaka_policy_capability import PolicyGuard, StaticPolicyProvider


def _request(invocation_id: str) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "policy contract test"},
        idempotency_key=f"key-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        policy_context={"network": "deny"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect", "error_code"),
    [
        (PolicyEffect.DENY, "policy.denied"),
        (PolicyEffect.REQUIRE_APPROVAL, "policy.approval_required"),
    ],
)
async def test_policy_guard_rejects_before_provider_start(
    effect: PolicyEffect,
    error_code: str,
) -> None:
    provider = FakeAgentProvider()
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)
    runtime.add_guard(
        PolicyGuard(StaticPolicyProvider(PolicyDecision(effect, "blocked by test policy")))
    )

    result = await (
        await runtime.submit(_request(f"inv-{effect.value}"), provider_id="fake-agent")
    ).wait()

    assert result.status is InvocationStatus.REJECTED
    assert result.error_code == error_code
    assert provider.starts == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_policy_guard_allows_provider_start() -> None:
    provider = FakeAgentProvider()
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)
    runtime.add_guard(
        PolicyGuard(StaticPolicyProvider(PolicyDecision(PolicyEffect.ALLOW, "allowed by test")))
    )

    result = await (await runtime.submit(_request("inv-allow"), provider_id="fake-agent")).wait()

    assert result.status is InvocationStatus.SUCCEEDED
    assert provider.starts == 1
    await runtime.stop()
