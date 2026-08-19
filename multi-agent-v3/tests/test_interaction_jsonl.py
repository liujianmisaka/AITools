from __future__ import annotations

from pathlib import Path

import pytest
from misaka_interaction_capability import MessageConflict
from misaka_interaction_contracts import (
    InteractionChannelRef,
    InteractionMessageDraft,
    MessageDeliveryStatus,
    MessageType,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_interaction_jsonl import JsonlInteractionChannelStore
from misaka_persistence_contracts import DurableCorruption
from misaka_persistence_jsonl import JsonlEventLog


def _channel() -> InteractionChannelRef:
    return InteractionChannelRef("channel-1", ScopeRef("scope-1"))


def _draft(message_id: str = "message-1") -> InteractionMessageDraft:
    return InteractionMessageDraft(
        message_id=message_id,
        channel_id="channel-1",
        sender=PrincipalRef("parent", PrincipalKind.AGENT),
        message_type=MessageType.INSTRUCTION,
        payload={"text": "inspect"},
        scope=ScopeRef("scope-1"),
    )


@pytest.mark.asyncio
async def test_jsonl_interaction_store_rebuilds_messages_and_delivery_facts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "interaction.jsonl"
    store = JsonlInteractionChannelStore(JsonlEventLog(path))
    created = await store.create(_channel())
    await store.publish(_draft())
    await store.transition("channel-1", "message-1", MessageDeliveryStatus.DELIVERED)
    await store.transition("channel-1", "message-1", MessageDeliveryStatus.PROCESSED)
    await store.transition("channel-1", "message-1", MessageDeliveryStatus.COMPLETED)
    await store.close("channel-1")

    reopened = JsonlInteractionChannelStore(JsonlEventLog(path))
    message = await reopened.get_message("channel-1", "message-1")
    snapshot = await reopened.snapshot("channel-1")

    assert message.delivery_status is MessageDeliveryStatus.COMPLETED
    assert message.sequence == 1
    assert snapshot.closed is True
    assert snapshot.last_sequence == 1
    assert snapshot.created_at == created.created_at


@pytest.mark.asyncio
async def test_jsonl_interaction_store_preserves_idempotency_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "interaction.jsonl"
    first = JsonlInteractionChannelStore(JsonlEventLog(path))
    await first.create(_channel())
    original = await first.publish(_draft())

    reopened = JsonlInteractionChannelStore(JsonlEventLog(path))
    duplicate = await reopened.publish(_draft())
    assert duplicate == original

    conflicting = InteractionMessageDraft(
        message_id="message-1",
        channel_id="channel-1",
        sender=original.sender,
        message_type=MessageType.QUESTION,
        payload={"text": "different"},
        scope=original.scope,
    )
    with pytest.raises(MessageConflict):
        await reopened.publish(conflicting)


@pytest.mark.asyncio
async def test_jsonl_interaction_store_rejects_unknown_channel_fact(tmp_path: Path) -> None:
    path = tmp_path / "interaction.jsonl"
    log = JsonlEventLog(path)
    await log.append(
        "interaction.channel:channel-1",
        "unknown-1",
        "interaction.unknown",
        {},
    )

    with pytest.raises(DurableCorruption, match="unknown interaction event"):
        await JsonlInteractionChannelStore(JsonlEventLog(path)).open()


@pytest.mark.asyncio
async def test_jsonl_interaction_store_rejects_duplicate_channel_creation_fact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "interaction.jsonl"
    log = JsonlEventLog(path)
    await log.append(
        "interaction.channel:channel-1",
        "channel-created",
        "interaction.channel.created",
        {
            "channel_id": "channel-1",
            "scope_id": "scope-1",
            "parent_scope_id": None,
            "created_at": "2026-08-19T00:00:00+00:00",
        },
    )
    await log.append(
        "interaction.channel:channel-1",
        "channel-created-again",
        "interaction.channel.created",
        {
            "channel_id": "channel-1",
            "scope_id": "scope-1",
            "parent_scope_id": None,
            "created_at": "2026-08-19T00:00:01+00:00",
        },
    )

    with pytest.raises(DurableCorruption, match="duplicate creation"):
        await JsonlInteractionChannelStore(JsonlEventLog(path)).open()
