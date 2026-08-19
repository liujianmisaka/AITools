from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from misaka_interaction_capability import (
    ChannelClosed,
    ChannelConflict,
    DeliveryConflict,
    MessageConflict,
)
from misaka_interaction_contracts import (
    InteractionChannelRef,
    InteractionMessageDraft,
    MessageCursor,
    MessageDeliveryStatus,
    MessageType,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_interaction_memory import (
    MemoryInteractionChannelModule,
    MemoryInteractionChannelStore,
)
from misaka_kernel import Host, HostStatus


def _channel(channel_id: str = "channel-1") -> InteractionChannelRef:
    return InteractionChannelRef(channel_id, ScopeRef("scope-1"))


def _draft(message_id: str, *, channel_id: str = "channel-1") -> InteractionMessageDraft:
    return InteractionMessageDraft(
        message_id=message_id,
        channel_id=channel_id,
        sender=PrincipalRef("parent", PrincipalKind.AGENT),
        message_type=MessageType.INSTRUCTION,
        payload={"text": message_id},
        scope=ScopeRef("scope-1"),
    )


@pytest.mark.asyncio
async def test_memory_channel_assigns_sequences_and_replays_from_cursor() -> None:
    store = MemoryInteractionChannelStore()
    await store.create(_channel())

    first = await store.publish(_draft("message-1"))
    second = await store.publish(_draft("message-2"))

    assert (first.sequence, second.sequence) == (1, 2)
    replay = await store.read("channel-1", cursor=MessageCursor("channel-1", 2))
    assert [message.message_id for message in replay] == ["message-2"]


@pytest.mark.asyncio
async def test_memory_channel_duplicate_publish_is_idempotent_and_conflicts_are_rejected() -> None:
    store = MemoryInteractionChannelStore()
    await store.create(_channel())

    original = await store.publish(_draft("message-1"))
    duplicate = await store.publish(_draft("message-1"))
    assert duplicate == original

    conflicting = InteractionMessageDraft(
        message_id="message-1",
        channel_id="channel-1",
        sender=original.sender,
        message_type=MessageType.QUESTION,
        payload={"text": "different"},
        scope=original.scope,
    )
    with pytest.raises(MessageConflict, match="different content"):
        await store.publish(conflicting)


@pytest.mark.asyncio
async def test_duplicate_message_identity_does_not_depend_on_creation_clock() -> None:
    store = MemoryInteractionChannelStore()
    await store.create(_channel())
    original = _draft("message-clock")
    first = await store.publish(original)
    later = _draft("message-clock")
    later = InteractionMessageDraft(
        message_id=later.message_id,
        channel_id=later.channel_id,
        sender=later.sender,
        message_type=later.message_type,
        payload=later.payload,
        scope=later.scope,
        created_at=first.created_at + timedelta(seconds=1),
    )

    assert await store.publish(later) == first


@pytest.mark.asyncio
async def test_concurrent_duplicate_publish_has_one_durable_sequence() -> None:
    store = MemoryInteractionChannelStore()
    await store.create(_channel())

    results = await asyncio.gather(
        store.publish(_draft("message-1")),
        store.publish(_draft("message-1")),
    )

    assert results[0] == results[1]
    assert results[0].sequence == 1
    assert (await store.snapshot("channel-1")).last_sequence == 1


@pytest.mark.asyncio
async def test_delivery_status_requires_monotonic_transition_and_expected_version() -> None:
    store = MemoryInteractionChannelStore()
    await store.create(_channel())
    await store.publish(_draft("message-1"))

    delivered = await store.transition(
        "channel-1",
        "message-1",
        MessageDeliveryStatus.DELIVERED,
        expected_status=MessageDeliveryStatus.ACCEPTED,
    )
    assert delivered.delivery_status is MessageDeliveryStatus.DELIVERED

    with pytest.raises(DeliveryConflict, match="expected accepted"):
        await store.transition(
            "channel-1",
            "message-1",
            MessageDeliveryStatus.PROCESSED,
            expected_status=MessageDeliveryStatus.ACCEPTED,
        )
    with pytest.raises(DeliveryConflict, match="cannot transition"):
        await store.transition("channel-1", "message-1", MessageDeliveryStatus.ACCEPTED)


@pytest.mark.asyncio
async def test_stream_waits_for_messages_and_finishes_after_close() -> None:
    store = MemoryInteractionChannelStore()
    await store.create(_channel())

    async def collect() -> list[str]:
        return [message.message_id async for message in store.events("channel-1")]

    consumer = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await store.publish(_draft("message-1"))
    await store.close("channel-1")

    assert await consumer == ["message-1"]
    with pytest.raises(ChannelClosed):
        await store.publish(_draft("message-2"))


@pytest.mark.asyncio
async def test_channel_scope_is_immutable_and_module_registers_store() -> None:
    store = MemoryInteractionChannelStore()
    await store.create(_channel())
    wrong_scope = InteractionMessageDraft(
        message_id="message-1",
        channel_id="channel-1",
        sender=PrincipalRef("parent", PrincipalKind.AGENT),
        message_type=MessageType.INSTRUCTION,
        payload={"text": "wrong"},
        scope=ScopeRef("other-scope"),
    )
    with pytest.raises(ChannelConflict, match="scope"):
        await store.publish(wrong_scope)

    host = Host()
    host.add_module(MemoryInteractionChannelModule())
    await host.start()
    assert host.status is HostStatus.ACTIVE
    await host.stop()
    assert host.status is HostStatus.STOPPED
