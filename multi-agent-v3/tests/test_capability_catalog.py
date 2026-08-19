from __future__ import annotations

import asyncio

import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE, agent_descriptor
from misaka_capability_catalog import (
    CapabilityCatalogAmbiguous,
    MemoryCapabilityCatalog,
    ProviderRegistrationConflict,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_contracts import CompletionBoundary, InvocationRequest, InvocationStatus
from misaka_invocation_runtime import InvocationRuntime


def _request(invocation_id: str) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=invocation_id,
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "catalog test"},
        idempotency_key=f"key-{invocation_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
    )


def test_memory_catalog_tracks_epochs_and_resolves_by_provider() -> None:
    catalog = MemoryCapabilityCatalog()
    first = catalog.register(
        "provider-a",
        agent_descriptor(),
        owner_id="module-a",
        scope_id="scope-a",
    )
    second = catalog.register(
        "provider-b",
        agent_descriptor(),
        owner_id="module-b",
        scope_id="scope-b",
    )

    assert [item.provider_id for item in catalog.snapshot()] == ["provider-a", "provider-b"]
    with pytest.raises(CapabilityCatalogAmbiguous):
        catalog.resolve(AGENT_CAPABILITY_ID)
    assert catalog.resolve(AGENT_CAPABILITY_ID, provider_id="provider-b") == second.registration

    async def dispose_first() -> None:
        await first.dispose()

    asyncio.run(dispose_first())
    replacement = catalog.register(
        "provider-a",
        agent_descriptor(),
        owner_id="module-a2",
        scope_id="scope-a2",
    )
    assert replacement.registration.epoch == 2
    resolved = catalog.resolve(AGENT_CAPABILITY_ID, provider_id="provider-a")
    assert resolved == replacement.registration


@pytest.mark.asyncio
async def test_memory_catalog_disposer_is_idempotent_and_runs_cleanup_once() -> None:
    catalog = MemoryCapabilityCatalog()
    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    handle = catalog.register(
        "provider",
        agent_descriptor(),
        owner_id="module",
        scope_id="scope",
        cleanup=cleanup,
    )
    await asyncio.gather(handle.dispose(), handle.dispose(), handle.dispose())
    assert cleanup_calls == 1
    assert catalog.snapshot() == ()


def test_memory_catalog_rejects_duplicate_provider() -> None:
    catalog = MemoryCapabilityCatalog()
    catalog.register(
        "provider",
        agent_descriptor(),
        owner_id="module",
        scope_id="scope",
    )
    with pytest.raises(ProviderRegistrationConflict):
        catalog.register(
            "provider",
            agent_descriptor(),
            owner_id="module-2",
            scope_id="scope-2",
        )


@pytest.mark.asyncio
async def test_runtime_removes_provider_for_new_work_but_preserves_accepted_binding() -> None:
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "ok"}, delay_seconds=0.02))
    runtime = InvocationRuntime()
    disposer = await runtime.register_provider(
        "fake",
        provider,
        owner_id="module",
        scope_id="scope",
    )
    accepted = await runtime.submit(_request("accepted"), provider_id="fake")
    await disposer()

    assert runtime.descriptors() == ()
    accepted_result = await accepted.wait()
    assert accepted_result.status is InvocationStatus.SUCCEEDED
    assert provider.starts == 1

    rejected = await runtime.submit(_request("rejected"), provider_id="fake")
    rejected_result = await rejected.wait()
    assert rejected_result.status is InvocationStatus.REJECTED
    assert provider.starts == 1
