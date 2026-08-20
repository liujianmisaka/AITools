from __future__ import annotations

import asyncio
from typing import cast

import pytest
from misaka_approval_capability import DecisionGate, MemoryDecisionStore
from misaka_interaction_contracts import (
    DecisionProposal,
    DecisionRef,
    DecisionStatus,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_kernel import ProfileDefinition, ProfileLoader
from misaka_kernel_contracts import JsonObject
from misaka_policy_capability import StaticPolicyProvider
from misaka_policy_contracts import PolicyDecision, PolicyEffect
from misaka_resource_capability import (
    MemoryCredentialProvider,
    MemoryResourceLeaseProvider,
    MemorySettingsProvider,
    StaticSandboxProvider,
)
from misaka_resource_contracts import (
    CredentialRef,
    FilesystemAccess,
    LeaseRequest,
    NetworkAccess,
    ResourceRef,
    SandboxCapabilities,
    SandboxRequirements,
    SettingsDefinition,
)
from misaka_tool_capability import (
    MEMORY_TOOL_MODULE_ID,
    TOOL_CAPABILITY_ID,
    TOOL_PIPELINE_MODULE_ID,
    TOOL_PIPELINE_SERVICE,
    TOOL_PROVIDER_SERVICE,
    MemoryToolModule,
    MemoryToolProvider,
    ToolDescriptor,
    ToolExecutionContext,
    ToolExecutionPipeline,
    ToolExecutionPipelineModule,
    ToolExecutionRequest,
    ToolInvocation,
    ToolStatus,
)

OWNER = PrincipalRef("tool-caller", PrincipalKind.APPLICATION)
SCOPE = ScopeRef("tool-scope")


def _add_descriptor(*, destructive: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id="math.add",
        display_name="Add numbers",
        description="Add two integers",
        input_schema={
            "type": "object",
            "required": ["left", "right"],
            "properties": {
                "left": {"type": "integer"},
                "right": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        output_schema={"type": "integer"},
        destructive=destructive,
    )


def _proposal(*, revision: int = 1) -> DecisionProposal:
    return DecisionProposal(
        ref=DecisionRef("tool-proposal", revision),
        plan_hash=("a" if revision == 1 else "b") * 64,
        requested_effects=("tool.execute:math.add",),
        scope=SCOPE,
        created_by=OWNER,
        policy_snapshot={"network": "deny"},
    )


def _request(
    invocation_id: str = "tool-inv-1",
    *,
    arguments: JsonObject | None = None,
    proposal: DecisionProposal | None = None,
    leases: tuple[LeaseRequest, ...] = (),
    credential_refs: tuple[CredentialRef, ...] = (),
    settings_ids: tuple[str, ...] = (),
) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        invocation=ToolInvocation(
            invocation_id,
            "math.add",
            arguments or {"left": 2, "right": 3},
            "key-1",
        ),
        proposal=proposal or _proposal(),
        sandbox=SandboxRequirements(
            filesystem=FilesystemAccess.NONE,
            network=NetworkAccess.DENY,
        ),
        lease_requests=leases,
        credential_refs=credential_refs,
        settings_ids=settings_ids,
    )


def _pipeline(
    provider: MemoryToolProvider,
    *,
    effect: PolicyEffect = PolicyEffect.ALLOW,
    store: MemoryDecisionStore | None = None,
    credentials: MemoryCredentialProvider | None = None,
    settings: MemorySettingsProvider | None = None,
) -> ToolExecutionPipeline:
    decision_store = store or MemoryDecisionStore()
    return ToolExecutionPipeline(
        provider,
        policy=StaticPolicyProvider(PolicyDecision(effect, "test policy")),
        decision_gate=DecisionGate(decision_store),
        leases=MemoryResourceLeaseProvider(),
        sandbox=StaticSandboxProvider(
            SandboxCapabilities(
                filesystem=FilesystemAccess.WRITE,
                network=NetworkAccess.DENY,
            )
        ),
        credentials=credentials,
        settings=settings,
    )


@pytest.mark.asyncio
async def test_tool_pipeline_validates_executes_and_deduplicates() -> None:
    calls = 0

    async def add(arguments: JsonObject, context: ToolExecutionContext) -> int:
        nonlocal calls
        del context
        calls += 1
        await asyncio.sleep(0)
        return cast(int, arguments["left"]) + cast(int, arguments["right"])

    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(), add)
    pipeline = _pipeline(provider)
    descriptor = await provider.describe()
    assert descriptor.capability_id == TOOL_CAPABILITY_ID

    first = await pipeline.execute(_request())
    second = await pipeline.execute(_request())

    assert first.status is ToolStatus.SUCCEEDED
    assert first.output == 5
    assert second == first
    assert calls == 1
    await pipeline.close()


@pytest.mark.asyncio
async def test_tool_pipeline_merges_concurrent_idempotent_calls() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_add(arguments: JsonObject, context: ToolExecutionContext) -> int:
        nonlocal calls
        del arguments, context
        calls += 1
        started.set()
        await release.wait()
        return 2

    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(), slow_add)
    pipeline = _pipeline(provider)
    first = asyncio.create_task(pipeline.execute(_request("tool-inv-1")))
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(pipeline.execute(_request("tool-inv-2")))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.status is ToolStatus.SUCCEEDED
    assert second_result == first_result
    await pipeline.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cache_unknown_result_over_shared_task() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_add(arguments: JsonObject, context: ToolExecutionContext) -> int:
        del arguments, context
        started.set()
        await release.wait()
        return 9

    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(), slow_add)
    pipeline = _pipeline(provider)
    request = _request("tool-inv-cancel-waiter")
    waiter = asyncio.create_task(pipeline.execute(request))
    await asyncio.wait_for(started.wait(), timeout=1)
    waiter.cancel()
    interrupted = await waiter
    assert interrupted.status is ToolStatus.RECONCILIATION_REQUIRED

    release.set()
    final = await pipeline.execute(request)
    assert final.status is ToolStatus.SUCCEEDED
    assert final.output == 9
    await pipeline.close()


@pytest.mark.asyncio
async def test_tool_pipeline_rejects_schema_and_idempotency_conflicts_before_provider() -> None:
    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(), lambda arguments, context: 1)
    pipeline = _pipeline(provider)

    invalid = await pipeline.execute(_request(arguments={"left": 2}))
    conflict = await pipeline.execute(_request(arguments={"left": 2, "right": 4}))

    assert invalid.status is ToolStatus.REJECTED
    assert invalid.error_code == "tool.input_contract_violated"
    assert conflict.error_code == "tool.idempotency_conflict"
    assert provider.executions == 0
    await pipeline.close()


@pytest.mark.asyncio
async def test_tool_pipeline_requires_decision_and_lease_before_destructive_effect() -> None:
    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(destructive=True), lambda args, ctx: 5)
    store = MemoryDecisionStore()
    pipeline = _pipeline(provider, effect=PolicyEffect.REQUIRE_DECISION, store=store)
    pending = await pipeline.execute(_request())
    assert pending.error_code == "policy.decision_required"
    assert provider.executions == 0

    proposal = _proposal(revision=2)
    await store.ensure(proposal)
    await store.decide(
        proposal.ref,
        status=DecisionStatus.APPROVED,
        decided_by=PrincipalRef("reviewer", PrincipalKind.HUMAN),
    )
    missing_lease = await pipeline.execute(
        ToolExecutionRequest(
            invocation=ToolInvocation(
                "tool-inv-2",
                "math.add",
                {"left": 2, "right": 3},
                "key-2",
            ),
            proposal=proposal,
            sandbox=SandboxRequirements(),
        )
    )
    assert missing_lease.error_code == "tool.resource_lease_required"
    assert provider.executions == 0
    await pipeline.close()


@pytest.mark.asyncio
async def test_tool_pipeline_resolves_credentials_settings_and_blocks_secret_output() -> None:
    credentials = MemoryCredentialProvider()
    credential_ref = CredentialRef("service-token", "tool authentication")
    await credentials.configure(credential_ref, "sensitive-value")
    settings = MemorySettingsProvider()
    await settings.define(
        SettingsDefinition(
            "tool-settings",
            schema={
                "type": "object",
                "required": ["region"],
                "properties": {"region": {"type": "string"}},
                "additionalProperties": False,
            },
            defaults={"region": "local"},
        )
    )

    def leak_secret(arguments: JsonObject, context: ToolExecutionContext) -> str:
        del arguments
        assert context.setting("tool-settings").values["region"] == "local"
        return context.credential("service-token").secret.reveal()

    provider = MemoryToolProvider()
    provider.register(
        "math.add",
        ToolDescriptor(
            tool_id="math.add",
            display_name="Credential test",
            description="Try to leak a credential",
            input_schema={"type": "object"},
            output_schema={"type": "string"},
        ),
        leak_secret,
    )
    pipeline = _pipeline(
        provider,
        credentials=credentials,
        settings=settings,
    )
    result = await pipeline.execute(
        _request(
            arguments={},
            credential_refs=(credential_ref,),
            settings_ids=("tool-settings",),
        )
    )

    assert result.status is ToolStatus.FAILED
    assert result.error_code == "tool.secret_output_forbidden"
    assert "sensitive-value" not in (result.error_message or "")
    await pipeline.close()


@pytest.mark.asyncio
async def test_tool_pipeline_releases_leases_when_admission_is_cancelled() -> None:
    class CancelledSandbox:
        async def resolve(self, requirements: SandboxRequirements) -> object:
            del requirements
            raise asyncio.CancelledError

    lease_provider = MemoryResourceLeaseProvider()
    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(), lambda arguments, context: 1)
    pipeline = ToolExecutionPipeline(
        provider,
        policy=StaticPolicyProvider(PolicyDecision(PolicyEffect.ALLOW, "test policy")),
        decision_gate=DecisionGate(MemoryDecisionStore()),
        leases=lease_provider,
        sandbox=CancelledSandbox(),  # type: ignore[arg-type]
    )
    lease_request = LeaseRequest(
        resource=ResourceRef("workspace", "cancelled-tool", SCOPE),
        owner=OWNER,
        operation_id="tool-inv-cancelled",
        ttl_seconds=30,
    )
    result = await pipeline.execute(_request("tool-inv-cancelled", leases=(lease_request,)))
    assert result.status is ToolStatus.RECONCILIATION_REQUIRED
    replacement = await lease_provider.acquire(
        LeaseRequest(
            resource=lease_request.resource,
            owner=PrincipalRef("replacement", PrincipalKind.APPLICATION),
            operation_id="replacement",
            ttl_seconds=30,
        )
    )
    assert replacement.epoch == 2
    await pipeline.close()


@pytest.mark.asyncio
async def test_memory_tool_module_binds_executor_to_kernel() -> None:
    provider = MemoryToolProvider()
    module = MemoryToolModule(provider)
    loader = ProfileLoader({MEMORY_TOOL_MODULE_ID: lambda: module})
    host = loader.create_host(
        ProfileDefinition(
            profile_id="tool-test",
            module_ids=(MEMORY_TOOL_MODULE_ID,),
        )
    )
    await host.start()
    try:
        assert host.services.require(TOOL_PROVIDER_SERVICE) is provider
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_tool_pipeline_module_binds_admission_boundary() -> None:
    provider = MemoryToolProvider()
    provider_module = MemoryToolModule(provider)
    pipeline = _pipeline(provider)
    pipeline_module = ToolExecutionPipelineModule(pipeline)
    loader = ProfileLoader(
        {
            MEMORY_TOOL_MODULE_ID: lambda: provider_module,
            TOOL_PIPELINE_MODULE_ID: lambda: pipeline_module,
        }
    )
    host = loader.create_host(
        ProfileDefinition(
            profile_id="tool-pipeline-test",
            module_ids=(MEMORY_TOOL_MODULE_ID, TOOL_PIPELINE_MODULE_ID),
        )
    )
    await host.start()
    assert host.services.require(TOOL_PIPELINE_SERVICE) is pipeline
    await host.stop()
    assert host.services.snapshot() == ()
