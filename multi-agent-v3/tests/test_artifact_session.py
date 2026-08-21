from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from misaka_artifact_capability import (
    ARTIFACT_STORE_SERVICE,
    ArtifactWrite,
    MemoryArtifactStore,
    MemoryArtifactStoreModule,
)
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
from misaka_invocation_contracts import SessionRef
from misaka_kernel import Host
from misaka_resource_capability import MemoryResourceLeaseProvider
from misaka_resource_contracts import LeaseRequest, ResourceRef
from misaka_session_capability import (
    MEMORY_SESSION_MODULE_ID,
    SESSION_STORE_SERVICE,
    MemorySessionStore,
    MemorySessionStoreModule,
    SessionLeaseBusy,
    SessionLeaseExpired,
    SessionLeaseFenced,
)


class _MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.mark.asyncio
async def test_memory_artifact_store_deduplicates_by_content_and_round_trips() -> None:
    owner = PrincipalRef("artifact-test", PrincipalKind.APPLICATION)
    scope = ScopeRef("artifact-scope")
    leases = MemoryResourceLeaseProvider()
    lease = await leases.acquire(
        LeaseRequest(ResourceRef("artifact", "artifact-1", scope), owner, "artifact-1")
    )
    store = MemoryArtifactStore()
    first = await store.put(
        ArtifactWrite(
            artifact_key="artifact-1",
            content=b"hello",
            media_type="text/plain",
            owner=owner,
            lease=lease,
            metadata={"role": "input"},
        )
    )
    second = await store.put(
        ArtifactWrite(
            artifact_key="artifact-1",
            content=b"hello",
            media_type="text/plain",
            owner=owner,
            lease=lease,
        )
    )

    assert first.artifact.artifact_id == second.artifact.artifact_id
    assert first.artifact.size_bytes == 5
    assert first.artifact.metadata["role"] == "input"
    assert await store.get(first.artifact) == b"hello"


@pytest.mark.asyncio
async def test_memory_session_store_renews_transfers_and_fences_leases() -> None:
    clock = _MutableClock()
    store = MemorySessionStore(clock=clock)
    record = await store.create("fake-agent", native_id="native-1")
    acquired = await store.acquire(
        record.session,
        "owner-a",
        "operation-a",
        ttl_seconds=10,
    )

    assert acquired.epoch == 1
    stored = await store.get(record.session)
    assert stored is not None
    assert stored.lease == acquired
    assert (
        await store.acquire(
            record.session,
            "owner-a",
            "operation-a",
            ttl_seconds=10,
        )
        == acquired
    )
    with pytest.raises(SessionLeaseBusy):
        await store.acquire(
            record.session,
            "owner-a",
            "operation-b",
            ttl_seconds=10,
        )

    clock.value += timedelta(seconds=4)
    renewed = await store.renew(acquired, ttl_seconds=10)
    assert renewed.epoch == acquired.epoch
    assert renewed.token == acquired.token
    assert renewed.expires_at > acquired.expires_at

    transferred = await store.transfer(
        renewed,
        "owner-b",
        "operation-b",
        ttl_seconds=10,
    )
    assert transferred.epoch == renewed.epoch + 1
    with pytest.raises(SessionLeaseFenced):
        await store.validate(renewed)
    with pytest.raises(SessionLeaseFenced):
        await store.release(renewed)

    released = await store.release(transferred)
    assert released.lease is None
    reclaimed = await store.acquire(
        SessionRef("fake-agent", "native-1"),
        "owner-c",
        "operation-c",
    )
    assert reclaimed.epoch == transferred.epoch + 1


@pytest.mark.asyncio
async def test_memory_session_store_expiry_allows_fenced_takeover() -> None:
    clock = _MutableClock()
    store = MemorySessionStore(clock=clock)
    session = (await store.create("fake-agent", native_id="native-expiring")).session
    first = await store.acquire(session, "owner-a", "operation-a", ttl_seconds=5)

    clock.value += timedelta(seconds=6)
    with pytest.raises(SessionLeaseExpired):
        await store.validate(first)
    second = await store.acquire(session, "owner-b", "operation-b", ttl_seconds=5)

    assert second.epoch == first.epoch + 1
    with pytest.raises(SessionLeaseFenced):
        await store.validate(first)


@pytest.mark.asyncio
async def test_artifact_and_session_modules_register_services_in_a_host() -> None:
    host = Host()
    host.add_module(MemoryArtifactStoreModule())
    session_module = MemorySessionStoreModule()
    host.add_module(session_module)

    await host.start()

    assert host.services.require(ARTIFACT_STORE_SERVICE) is not None
    assert host.services.require(SESSION_STORE_SERVICE) is session_module.store
    assert MEMORY_SESSION_MODULE_ID in {
        module.manifest.module_id for module in (MemorySessionStoreModule(),)
    }
    await host.stop()
