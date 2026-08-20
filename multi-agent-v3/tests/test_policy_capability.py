from __future__ import annotations

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_approval_capability import DecisionGate, MemoryDecisionStore
from misaka_fake_agent import FakeAgentProvider
from misaka_interaction_contracts import DecisionStatus, PrincipalKind, PrincipalRef
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationRequest,
    InvocationStatus,
)
from misaka_invocation_runtime import InvocationRuntime
from misaka_policy_capability import (
    PolicyGuard,
    StaticPolicyProvider,
    invocation_decision_proposal,
)
from misaka_policy_contracts import PolicyDecision, PolicyEffect


def _request(invocation_id: str, *, revision: int = 1) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "policy contract test"},
        idempotency_key=f"key-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        policy_context={
            "network_policy": "deny",
            "plan_revision": revision,
            "api_token": "must-not-be-persisted",
        },
    )


@pytest.mark.asyncio
async def test_policy_guard_denies_before_provider_start() -> None:
    provider = FakeAgentProvider()
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)
    runtime.add_guard(
        PolicyGuard(StaticPolicyProvider(PolicyDecision(PolicyEffect.DENY, "blocked by policy")))
    )

    result = await (await runtime.submit(_request("inv-deny"), provider_id="fake-agent")).wait()

    assert result.status is InvocationStatus.REJECTED
    assert result.error_code == "policy.denied"
    assert provider.starts == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_policy_requires_bound_decision_before_provider_start() -> None:
    provider = FakeAgentProvider()
    store = MemoryDecisionStore()
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)
    runtime.add_guard(
        PolicyGuard(
            StaticPolicyProvider(
                PolicyDecision(PolicyEffect.REQUIRE_DECISION, "human decision required")
            ),
            decision_gate=DecisionGate(store),
        )
    )
    request = _request("inv-decision")

    pending = await (await runtime.submit(request, provider_id="fake-agent")).wait()

    assert pending.status is InvocationStatus.REJECTED
    assert pending.error_code == "policy.decision_required"
    assert provider.starts == 0
    proposal = invocation_decision_proposal(request)
    stored = await store.get(proposal.ref)
    assert stored.proposal.policy_snapshot["api_token"] == "<redacted>"

    second_runtime = InvocationRuntime()
    second_provider = FakeAgentProvider()
    await second_runtime.register_provider("fake-agent", second_provider)
    await store.decide(
        proposal.ref,
        status=DecisionStatus.APPROVED,
        decided_by=PrincipalRef("reviewer", PrincipalKind.HUMAN),
    )
    second_runtime.add_guard(
        PolicyGuard(
            StaticPolicyProvider(
                PolicyDecision(PolicyEffect.REQUIRE_DECISION, "human decision required")
            ),
            decision_gate=DecisionGate(store),
        )
    )

    allowed = await (await second_runtime.submit(request, provider_id="fake-agent")).wait()

    assert allowed.status is InvocationStatus.SUCCEEDED
    assert second_provider.starts == 1
    await runtime.stop()
    await second_runtime.stop()


@pytest.mark.asyncio
async def test_old_decision_does_not_authorize_new_plan_revision() -> None:
    store = MemoryDecisionStore()
    first = _request("inv-revision", revision=1)
    first_proposal = invocation_decision_proposal(first)
    await store.ensure(first_proposal)
    await store.decide(
        first_proposal.ref,
        status=DecisionStatus.APPROVED,
        decided_by=PrincipalRef("reviewer", PrincipalKind.HUMAN),
    )
    provider = FakeAgentProvider()
    runtime = InvocationRuntime()
    await runtime.register_provider("fake-agent", provider)
    runtime.add_guard(
        PolicyGuard(
            StaticPolicyProvider(
                PolicyDecision(PolicyEffect.REQUIRE_DECISION, "human decision required")
            ),
            decision_gate=DecisionGate(store),
        )
    )

    result = await (
        await runtime.submit(_request("inv-revision", revision=2), provider_id="fake-agent")
    ).wait()

    assert result.status is InvocationStatus.REJECTED
    assert result.error_code == "policy.decision_required"
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
