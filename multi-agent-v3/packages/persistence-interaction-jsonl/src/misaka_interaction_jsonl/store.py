from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

from misaka_interaction_capability import (
    ChannelClosed,
    ChannelConflict,
    ChannelNotFound,
    MessageConflict,
    message_matches_draft,
    validate_delivery_transition,
)
from misaka_interaction_capability.ports import ChannelSnapshot
from misaka_interaction_contracts import (
    InteractionChannelRef,
    InteractionMessage,
    InteractionMessageDraft,
    MessageCursor,
    MessageDeliveryStatus,
    MessageType,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableCorruption
from misaka_persistence_jsonl import JsonlEventLog


class JsonlInteractionChannelStore:
    """Rebuildable interaction facts backed by the generic append-only JSONL log."""

    _STREAM_PREFIX = "interaction.channel:"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._memory = MemoryInteractionChannelStore()
        self._lock = asyncio.Lock()
        self._opened = False

    async def open(self) -> None:
        async with self._lock:
            if self._opened:
                return
            try:
                for event in await self._log.all_events():
                    if not event.stream_id.startswith(self._STREAM_PREFIX):
                        continue
                    await self._apply_event(event.stream_id, event.event_type, event.payload)
            except DurableCorruption:
                raise
            except Exception as exc:
                raise DurableCorruption(
                    "interaction.invalid_fact",
                    "interaction JSONL facts have an invalid shape or transition",
                ) from exc
            self._opened = True

    async def create(self, channel: InteractionChannelRef) -> ChannelSnapshot:
        await self.open()
        async with self._lock:
            try:
                existing = await self._memory.snapshot(channel.channel_id)
                if existing.ref != channel:
                    raise ChannelConflict(
                        "interaction.channel_conflict",
                        f"channel {channel.channel_id} already exists with a different scope",
                    )
                return existing
            except ChannelNotFound:
                pass
            created_at = datetime.now(UTC)
            await self._log.append(
                self._stream(channel.channel_id),
                "channel-created",
                "interaction.channel.created",
                _encode_channel(channel, created_at),
                occurred_at=created_at,
            )
            return await self._memory.create(channel, created_at=created_at)

    async def snapshot(self, channel_id: str) -> ChannelSnapshot:
        await self.open()
        return await self._memory.snapshot(channel_id)

    async def publish(self, draft: InteractionMessageDraft) -> InteractionMessage:
        await self.open()
        async with self._lock:
            existing = await self._memory.find_message(draft.message_id)
            if existing is not None:
                if existing.channel_id != draft.channel_id or not message_matches_draft(
                    existing, draft
                ):
                    raise MessageConflict(
                        "interaction.message_conflict",
                        f"message {draft.message_id} already exists with different content",
                    )
                return existing
            snapshot = await self._memory.snapshot(draft.channel_id)
            if snapshot.closed:
                raise ChannelClosed(
                    "interaction.channel_closed",
                    f"channel {draft.channel_id} is closed",
                )
            planned = draft.to_message(snapshot.last_sequence + 1)
            await self._log.append(
                self._stream(draft.channel_id),
                f"message-accepted:{draft.message_id}",
                "interaction.message.accepted",
                _encode_message(planned),
                occurred_at=planned.created_at,
            )
            restored = await self._memory.publish(draft)
            if restored != planned:
                raise DurableCorruption(
                    "interaction.sequence_mismatch",
                    f"message {draft.message_id} was assigned an unexpected sequence",
                )
            return restored

    async def get_message(self, channel_id: str, message_id: str) -> InteractionMessage:
        await self.open()
        return await self._memory.get_message(channel_id, message_id)

    async def find_message(self, message_id: str) -> InteractionMessage | None:
        await self.open()
        return await self._memory.find_message(message_id)

    async def transition(
        self,
        channel_id: str,
        message_id: str,
        status: MessageDeliveryStatus,
        *,
        expected_status: MessageDeliveryStatus | None = None,
    ) -> InteractionMessage:
        await self.open()
        async with self._lock:
            current = await self._memory.get_message(channel_id, message_id)
            validate_delivery_transition(
                current.delivery_status,
                status,
                expected=expected_status,
            )
            if status is current.delivery_status:
                return current
            await self._log.append(
                self._stream(channel_id),
                f"message-delivery:{message_id}:{status.value}",
                "interaction.message.delivery",
                {"message_id": message_id, "status": status.value},
            )
            return await self._memory.transition(
                channel_id,
                message_id,
                status,
                expected_status=expected_status,
            )

    async def read(
        self, channel_id: str, *, cursor: MessageCursor | None = None
    ) -> tuple[InteractionMessage, ...]:
        await self.open()
        return await self._memory.read(channel_id, cursor=cursor)

    def events(
        self, channel_id: str, *, cursor: MessageCursor | None = None
    ) -> AsyncIterator[InteractionMessage]:
        return self._events(channel_id, cursor=cursor)

    async def _events(
        self, channel_id: str, *, cursor: MessageCursor | None = None
    ) -> AsyncIterator[InteractionMessage]:
        await self.open()
        async for message in self._memory.events(channel_id, cursor=cursor):
            yield message

    async def close(self, channel_id: str) -> ChannelSnapshot:
        await self.open()
        async with self._lock:
            snapshot = await self._memory.snapshot(channel_id)
            if snapshot.closed:
                return snapshot
            closed_at = datetime.now(UTC)
            await self._log.append(
                self._stream(channel_id),
                "channel-closed",
                "interaction.channel.closed",
                {"closed_at": closed_at.isoformat()},
                occurred_at=closed_at,
            )
            return await self._memory.close(channel_id)

    async def _apply_event(
        self,
        stream_id: str,
        event_type: str,
        payload: JsonObject,
    ) -> None:
        channel_id = stream_id.removeprefix(self._STREAM_PREFIX)
        if not channel_id.strip():
            raise DurableCorruption(
                "interaction.channel_id_empty",
                "interaction event stream has an empty channel id",
            )
        if event_type == "interaction.channel.created":
            channel, created_at = _decode_channel(payload)
            if channel.channel_id != channel_id:
                raise DurableCorruption(
                    "interaction.channel_id_mismatch",
                    "channel fact does not match its event stream",
                )
            try:
                existing = await self._memory.snapshot(channel.channel_id)
            except ChannelNotFound:
                existing = None
            if existing is not None:
                raise DurableCorruption(
                    "interaction.channel_duplicate",
                    f"channel {channel.channel_id} has duplicate creation facts",
                )
            await self._memory.create(channel, created_at=created_at)
            return
        if event_type == "interaction.message.accepted":
            message = _decode_message(payload)
            if message.channel_id != channel_id:
                raise DurableCorruption(
                    "interaction.message_channel_mismatch",
                    "message fact does not match its event stream",
                )
            if message.delivery_status is not MessageDeliveryStatus.ACCEPTED:
                raise DurableCorruption(
                    "interaction.accepted_status_invalid",
                    "accepted message fact must have accepted delivery status",
                )
            if await self._memory.find_message(message.message_id) is not None:
                raise DurableCorruption(
                    "interaction.message_duplicate",
                    f"message {message.message_id} has duplicate accepted facts",
                )
            restored = await self._memory.publish(_draft_from_message(message))
            if restored != message:
                raise DurableCorruption(
                    "interaction.message_mismatch",
                    f"message {message.message_id} could not be rebuilt exactly",
                )
            return
        if event_type == "interaction.message.delivery":
            message_id = _required_string(payload, "message_id")
            raw_status = _required_string(payload, "status")
            status = MessageDeliveryStatus(raw_status)
            await self._memory.transition(channel_id, message_id, status)
            return
        if event_type == "interaction.channel.closed":
            snapshot = await self._memory.snapshot(channel_id)
            if snapshot.closed:
                raise DurableCorruption(
                    "interaction.channel_duplicate_close",
                    f"channel {channel_id} has duplicate close facts",
                )
            await self._memory.close(channel_id)
            return
        raise DurableCorruption(
            "interaction.event_type_unknown",
            f"unknown interaction event type {event_type}",
        )

    @classmethod
    def _stream(cls, channel_id: str) -> str:
        if not channel_id.strip():
            raise ValueError("channel_id must not be empty")
        return f"{cls._STREAM_PREFIX}{channel_id}"


def _encode_channel(channel: InteractionChannelRef, created_at: datetime) -> JsonObject:
    return {
        "channel_id": channel.channel_id,
        "scope_id": channel.scope.scope_id,
        "parent_scope_id": channel.scope.parent_scope_id,
        "created_at": created_at.isoformat(),
    }


def _encode_message(message: InteractionMessage) -> JsonObject:
    return {
        "message_id": message.message_id,
        "channel_id": message.channel_id,
        "sender": _encode_principal(message.sender),
        "message_type": message.message_type.value,
        "payload": message.payload,
        "sequence": message.sequence,
        "scope": _encode_scope(message.scope),
        "recipient": _encode_principal(message.recipient)
        if message.recipient is not None
        else None,
        "payload_schema": message.payload_schema,
        "correlation_id": message.correlation_id,
        "causation_id": message.causation_id,
        "reply_to": message.reply_to,
        "delivery_status": message.delivery_status.value,
        "created_at": message.created_at.isoformat(),
        "expires_at": message.expires_at.isoformat() if message.expires_at is not None else None,
    }


def _decode_channel(payload: JsonObject) -> tuple[InteractionChannelRef, datetime]:
    channel = InteractionChannelRef(
        _required_string(payload, "channel_id"),
        _decode_scope(
            {
                "scope_id": _required_string(payload, "scope_id"),
                "parent_scope_id": payload.get("parent_scope_id"),
            }
        ),
    )
    created_at = datetime.fromisoformat(_required_string(payload, "created_at"))
    return channel, created_at


def _decode_message(payload: JsonObject) -> InteractionMessage:
    return InteractionMessage(
        message_id=_required_string(payload, "message_id"),
        channel_id=_required_string(payload, "channel_id"),
        sender=_decode_principal(_required_object(payload, "sender")),
        message_type=MessageType(_required_string(payload, "message_type")),
        payload=_required_object(payload, "payload"),
        sequence=_required_int(payload, "sequence"),
        scope=_decode_scope(_required_object(payload, "scope")),
        recipient=(
            _decode_principal(cast(JsonObject, payload["recipient"]))
            if payload.get("recipient") is not None
            else None
        ),
        payload_schema=(
            cast(JsonObject, payload["payload_schema"])
            if payload.get("payload_schema") is not None
            else None
        ),
        correlation_id=_optional_string(payload.get("correlation_id")),
        causation_id=_optional_string(payload.get("causation_id")),
        reply_to=_optional_string(payload.get("reply_to")),
        delivery_status=MessageDeliveryStatus(_required_string(payload, "delivery_status")),
        created_at=datetime.fromisoformat(_required_string(payload, "created_at")),
        expires_at=(
            datetime.fromisoformat(cast(str, payload["expires_at"]))
            if payload.get("expires_at") is not None
            else None
        ),
    )


def _draft_from_message(message: InteractionMessage) -> InteractionMessageDraft:
    return InteractionMessageDraft(
        message_id=message.message_id,
        channel_id=message.channel_id,
        sender=message.sender,
        message_type=message.message_type,
        payload=message.payload,
        scope=message.scope,
        recipient=message.recipient,
        payload_schema=message.payload_schema,
        correlation_id=message.correlation_id,
        causation_id=message.causation_id,
        reply_to=message.reply_to,
        created_at=message.created_at,
        expires_at=message.expires_at,
    )


def _encode_principal(principal: PrincipalRef) -> JsonObject:
    return {
        "principal_id": principal.principal_id,
        "kind": principal.kind.value,
        "display_name": principal.display_name,
    }


def _decode_principal(payload: JsonObject) -> PrincipalRef:
    return PrincipalRef(
        principal_id=_required_string(payload, "principal_id"),
        kind=PrincipalKind(_required_string(payload, "kind")),
        display_name=_optional_string(payload.get("display_name")) or "",
    )


def _encode_scope(scope: ScopeRef) -> JsonObject:
    return {"scope_id": scope.scope_id, "parent_scope_id": scope.parent_scope_id}


def _decode_scope(payload: JsonObject) -> ScopeRef:
    return ScopeRef(
        scope_id=_required_string(payload, "scope_id"),
        parent_scope_id=_optional_string(payload.get("parent_scope_id")),
    )


def _required_object(payload: JsonObject, name: str) -> JsonObject:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(JsonObject, value)


def _required_string(payload: JsonObject, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional string field has an invalid value")
    return value


def _required_int(payload: JsonObject, name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value
