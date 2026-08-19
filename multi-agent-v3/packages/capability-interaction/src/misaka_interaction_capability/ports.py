from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from misaka_interaction_contracts import (
    InteractionChannelRef,
    InteractionMessage,
    InteractionMessageDraft,
    MessageCursor,
    MessageDeliveryStatus,
)
from misaka_kernel_contracts import ServiceKey

INTERACTION_CHANNEL_SERVICE = ServiceKey("capability.interaction.channel_store")


@dataclass(frozen=True, slots=True)
class ChannelSnapshot:
    ref: InteractionChannelRef
    last_sequence: int = 0
    closed: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.last_sequence < 0:
            raise ValueError("last_sequence must not be negative")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.closed_at is not None:
            if self.closed_at.tzinfo is None or self.closed_at.utcoffset() is None:
                raise ValueError("closed_at must be timezone-aware")
            if self.closed_at < self.created_at:
                raise ValueError("closed_at must not precede created_at")
        if self.closed and self.closed_at is None:
            raise ValueError("closed channels must have closed_at")
        if not self.closed and self.closed_at is not None:
            raise ValueError("open channels must not have closed_at")


class InteractionChannelStore(Protocol):
    async def create(self, channel: InteractionChannelRef) -> ChannelSnapshot: ...

    async def snapshot(self, channel_id: str) -> ChannelSnapshot: ...

    async def publish(self, draft: InteractionMessageDraft) -> InteractionMessage: ...

    async def get_message(self, channel_id: str, message_id: str) -> InteractionMessage: ...

    async def transition(
        self,
        channel_id: str,
        message_id: str,
        status: MessageDeliveryStatus,
        *,
        expected_status: MessageDeliveryStatus | None = None,
    ) -> InteractionMessage: ...

    async def read(
        self, channel_id: str, *, cursor: MessageCursor | None = None
    ) -> tuple[InteractionMessage, ...]: ...

    def events(
        self, channel_id: str, *, cursor: MessageCursor | None = None
    ) -> AsyncIterator[InteractionMessage]: ...

    async def close(self, channel_id: str) -> ChannelSnapshot: ...
