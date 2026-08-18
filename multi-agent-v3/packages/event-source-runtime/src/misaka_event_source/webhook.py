from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import cast

from misaka_kernel_contracts import JsonObject

from misaka_event_source.contracts import CloudEvent
from misaka_event_source.memory import MemoryEventSource


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    source: str
    event_type: str
    secret: bytes | None = None
    max_body_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.event_type.strip():
            raise ValueError("source and event_type must not be empty")
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")


class WebhookEventSource:
    def __init__(self, config: WebhookConfig) -> None:
        self.config = config
        self._source = MemoryEventSource()

    async def ingest(
        self,
        *,
        event_id: str,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> CloudEvent:
        if len(body) > self.config.max_body_bytes:
            raise ValueError("webhook payload exceeds configured limit")
        if self.config.secret is not None:
            signature = _header(headers or {}, "x-signature-256")
            expected = "sha256=" + hmac.new(self.config.secret, body, hashlib.sha256).hexdigest()
            if signature is None or not hmac.compare_digest(signature, expected):
                raise ValueError("webhook signature is invalid")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("webhook body must be valid UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("webhook body must be a JSON object")
        event = _cloud_event_from_payload(cast(dict[str, object], decoded), event_id, self.config)
        return await self._source.publish(event)

    async def events(self, *, start_sequence: int = 1):
        async for event in self._source.events(start_sequence=start_sequence):
            yield event

    async def close(self) -> None:
        await self._source.close()


def _cloud_event_from_payload(
    payload: dict[str, object], event_id: str, config: WebhookConfig
) -> CloudEvent:
    if {"specversion", "id", "source", "type", "data"} <= payload.keys():
        data = payload["data"]
        if not isinstance(data, dict):
            raise ValueError("CloudEvent data must be an object")
        return CloudEvent(
            event_id=str(payload["id"]),
            source=str(payload["source"]),
            event_type=str(payload["type"]),
            data=cast(JsonObject, data),
            subject=str(payload["subject"]) if payload.get("subject") else None,
        )
    return CloudEvent(
        event_id=event_id,
        source=config.source,
        event_type=config.event_type,
        data=cast(JsonObject, payload),
    )


def _header(headers: dict[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None
