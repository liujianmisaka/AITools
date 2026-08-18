from __future__ import annotations

import pytest
from misaka_artifact_capability import (
    ARTIFACT_STORE_SERVICE,
    MemoryArtifactStore,
    MemoryArtifactStoreModule,
)
from misaka_invocation_contracts import SessionRef
from misaka_kernel import Host
from misaka_session_capability import (
    MEMORY_SESSION_MODULE_ID,
    SESSION_STORE_SERVICE,
    MemorySessionStore,
    MemorySessionStoreModule,
    SessionBusyError,
)


@pytest.mark.asyncio
async def test_memory_artifact_store_deduplicates_by_content_and_round_trips() -> None:
    store = MemoryArtifactStore()
    first = await store.put(b"hello", media_type="text/plain", metadata={"role": "input"})
    second = await store.put(b"hello", media_type="text/plain")

    assert first.artifact_id == second.artifact_id
    assert first.size_bytes == 5
    assert first.metadata == {"role": "input"}
    assert await store.get(first) == b"hello"


@pytest.mark.asyncio
async def test_memory_session_store_serializes_claims() -> None:
    store = MemorySessionStore()
    record = await store.create("fake-agent", native_id="native-1")
    claimed = await store.claim(record.session, "owner-a")

    assert claimed.claimed_by == "owner-a"
    with pytest.raises(SessionBusyError):
        await store.claim(record.session, "owner-b")

    released = await store.release(record.session, "owner-a")
    assert released.claimed_by is None
    reclaimed = await store.claim(SessionRef("fake-agent", "native-1"), "owner-b")
    assert reclaimed.claimed_by == "owner-b"


@pytest.mark.asyncio
async def test_artifact_and_session_modules_register_services_in_a_host() -> None:
    host = Host()
    host.add_module(MemoryArtifactStoreModule())
    host.add_module(MemorySessionStoreModule())

    await host.start()

    assert host.services.require(ARTIFACT_STORE_SERVICE) is not None
    assert host.services.require(SESSION_STORE_SERVICE) is not None
    assert MEMORY_SESSION_MODULE_ID in {
        module.manifest.module_id for module in (MemorySessionStoreModule(),)
    }
    await host.stop()
