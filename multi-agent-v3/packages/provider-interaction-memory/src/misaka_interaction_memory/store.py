from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from misaka_interaction_capability import (
    ChannelClosed,
    ChannelConflict,
    ChannelNotFound,
    DeliveryConflict,
    MessageConflict,
    MessageNotFound,
)
from misaka_interaction_capability.ports import ChannelSnapshot
from misaka_interaction_contracts import (
    InteractionChannelRef,
    InteractionMessage,
    InteractionMessageDraft,
    MessageCursor,
    MessageDeliveryStatus,
)


@dataclass(slots=True)
class _ChannelRecord:
    ref: InteractionChannelRef
    created_at: datetime
    messages: list[InteractionMessage]
    by_id: dict[str, InteractionMessage]
    closed_at: datetime | None
    condition: asyncio.Condition


class MemoryInteractionChannelStore:
    """Concurrency-safe, provider-neutral interaction channel facts."""

    def __init__(self) -> None:
        self._channels: dict[str, _ChannelRecord] = {}
        self._message_channels: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, channel: InteractionChannelRef) -> ChannelSnapshot:
        async with self._lock:
            existing = self._channels.get(channel.channel_id)
            if existing is not None:
                if existing.ref != channel:
                    raise ChannelConflict(
                        "interaction.channel_conflict",
                        f"channel {channel.channel_id} already exists with a different scope",
                    )
                return _snapshot(existing)
            record = _ChannelRecord(
                ref=channel,
                created_at=datetime.now(UTC),
                messages=[],
                by_id={},
                closed_at=None,
                condition=asyncio.Condition(),
            )
            self._channels[channel.channel_id] = record
            return _snapshot(record)

    async def snapshot(self, channel_id: str) -> ChannelSnapshot:
        record = self._record(channel_id)
        async with record.condition:
            return _snapshot(record)

    async def publish(self, draft: InteractionMessageDraft) -> InteractionMessage:
        record = self._record(draft.channel_id)
        async with self._lock:
            async with record.condition:
                if record.closed_at is not None:
                    raise ChannelClosed(
                        "interaction.channel_closed",
                        f"channel {draft.channel_id} is closed",
                    )
                if draft.scope != record.ref.scope:
                    raise ChannelConflict(
                        "interaction.scope_mismatch",
                        f"message {draft.message_id} does not belong to channel scope",
                    )
                existing_channel = self._message_channels.get(draft.message_id)
                if existing_channel is not None:
                    existing = self._channels[existing_channel].by_id[draft.message_id]
                    expected = draft.to_message(existing.sequence)
                    accepted = replace(existing, delivery_status=MessageDeliveryStatus.ACCEPTED)
                    if existing != expected and accepted != expected:
                        raise MessageConflict(
                            "interaction.message_conflict",
                            f"message {draft.message_id} already exists with different content",
                        )
                    return existing
                message = draft.to_message(len(record.messages) + 1)
                record.messages.append(message)
                record.by_id[message.message_id] = message
                self._message_channels[message.message_id] = draft.channel_id
                record.condition.notify_all()
                return message

    async def get_message(self, channel_id: str, message_id: str) -> InteractionMessage:
        record = self._record(channel_id)
        async with record.condition:
            try:
                return record.by_id[message_id]
            except KeyError as exc:
                raise MessageNotFound(
                    "interaction.message_not_found",
                    f"message {message_id} was not found in channel {channel_id}",
                ) from exc

    async def transition(
        self,
        channel_id: str,
        message_id: str,
        status: MessageDeliveryStatus,
        *,
        expected_status: MessageDeliveryStatus | None = None,
    ) -> InteractionMessage:
        record = self._record(channel_id)
        async with record.condition:
            try:
                current = record.by_id[message_id]
            except KeyError as exc:
                raise MessageNotFound(
                    "interaction.message_not_found",
                    f"message {message_id} was not found in channel {channel_id}",
                ) from exc
            if expected_status is not None and current.delivery_status is not expected_status:
                raise DeliveryConflict(
                    "interaction.delivery_expected_mismatch",
                    f"message {message_id} is {current.delivery_status.value}, "
                    f"expected {expected_status.value}",
                )
            if status is current.delivery_status:
                return current
            if status not in _ALLOWED_DELIVERY_TRANSITIONS[current.delivery_status]:
                raise DeliveryConflict(
                    "interaction.delivery_transition_invalid",
                    f"message cannot transition from {current.delivery_status.value} "
                    f"to {status.value}",
                )
            updated = replace(current, delivery_status=status)
            record.messages[current.sequence - 1] = updated
            record.by_id[message_id] = updated
            record.condition.notify_all()
            return updated

    async def read(
        self, channel_id: str, *, cursor: MessageCursor | None = None
    ) -> tuple[InteractionMessage, ...]:
        record = self._record(channel_id)
        effective_cursor = cursor or MessageCursor(channel_id)
        if effective_cursor.channel_id != channel_id:
            raise ValueError("cursor channel does not match requested channel")
        async with record.condition:
            return tuple(record.messages[effective_cursor.next_sequence - 1 :])

    async def events(
        self, channel_id: str, *, cursor: MessageCursor | None = None
    ) -> AsyncIterator[InteractionMessage]:
        record = self._record(channel_id)
        effective_cursor = cursor or MessageCursor(channel_id)
        if effective_cursor.channel_id != channel_id:
            raise ValueError("cursor channel does not match requested channel")
        index = effective_cursor.next_sequence - 1
        while True:
            async with record.condition:
                while index >= len(record.messages) and record.closed_at is None:
                    await record.condition.wait()
                if index < len(record.messages):
                    message = record.messages[index]
                    index += 1
                else:
                    return
            yield message

    async def close(self, channel_id: str) -> ChannelSnapshot:
        record = self._record(channel_id)
        async with record.condition:
            if record.closed_at is None:
                record.closed_at = datetime.now(UTC)
                record.condition.notify_all()
            return _snapshot(record)

    def _record(self, channel_id: str) -> _ChannelRecord:
        if not channel_id.strip():
            raise ValueError("channel_id must not be empty")
        try:
            return self._channels[channel_id]
        except KeyError as exc:
            raise ChannelNotFound(
                "interaction.channel_not_found",
                f"channel {channel_id} was not found",
            ) from exc


def _snapshot(record: _ChannelRecord) -> ChannelSnapshot:
    return ChannelSnapshot(
        ref=record.ref,
        last_sequence=len(record.messages),
        closed=record.closed_at is not None,
        created_at=record.created_at,
        closed_at=record.closed_at,
    )


_ALLOWED_DELIVERY_TRANSITIONS: dict[MessageDeliveryStatus, frozenset[MessageDeliveryStatus]] = {
    MessageDeliveryStatus.ACCEPTED: frozenset(
        {
            MessageDeliveryStatus.DELIVERED,
            MessageDeliveryStatus.REJECTED,
            MessageDeliveryStatus.EXPIRED,
        }
    ),
    MessageDeliveryStatus.DELIVERED: frozenset(
        {
            MessageDeliveryStatus.PROCESSED,
            MessageDeliveryStatus.REJECTED,
            MessageDeliveryStatus.EXPIRED,
        }
    ),
    MessageDeliveryStatus.PROCESSED: frozenset({MessageDeliveryStatus.COMPLETED}),
    MessageDeliveryStatus.COMPLETED: frozenset(),
    MessageDeliveryStatus.REJECTED: frozenset(),
    MessageDeliveryStatus.EXPIRED: frozenset(),
}
