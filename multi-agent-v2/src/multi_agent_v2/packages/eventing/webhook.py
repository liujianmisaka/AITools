from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast

from multi_agent_v2.packages.credentials import CredentialProvider, CredentialRef
from multi_agent_v2.packages.domain.events import CloudEventEnvelope
from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue


class WebhookVerificationError(ValueError):
    """A webhook violates the configured admission policy."""


class WebhookPolicy:
    def __init__(
        self,
        *,
        credentials: CredentialProvider,
        secret_ref: CredentialRef | None,
        require_hmac: bool = True,
        maximum_body_bytes: int = 1_048_576,
        timestamp_tolerance_seconds: int = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if require_hmac and secret_ref is None:
            raise ValueError("required webhook HMAC policy needs a credential reference")
        if maximum_body_bytes < 1:
            raise ValueError("webhook maximum body size must be positive")
        if timestamp_tolerance_seconds < 1:
            raise ValueError("webhook timestamp tolerance must be positive")
        self._credentials = credentials
        self._secret_ref = secret_ref
        self._require_hmac = require_hmac
        self._maximum_body_bytes = maximum_body_bytes
        self._timestamp_tolerance_seconds = timestamp_tolerance_seconds
        self._clock = clock

    @property
    def maximum_body_bytes(self) -> int:
        return self._maximum_body_bytes

    async def verify(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        source_name: str,
    ) -> None:
        if len(body) > self._maximum_body_bytes:
            raise WebhookVerificationError("webhook payload exceeds the configured limit")
        _validate_source_name(source_name)
        normalized = {name.lower(): value.strip() for name, value in headers.items()}
        signature = normalized.get("x-misaka-signature")
        timestamp = normalized.get("x-misaka-timestamp")
        if not self._require_hmac and signature is None:
            return
        resolved = (
            await self._credentials.resolve(self._secret_ref)
            if self._secret_ref is not None
            else None
        )
        secret = resolved.value.get_secret_value().encode("utf-8") if resolved is not None else None
        nonce = normalized.get("x-misaka-nonce")
        if secret is None or signature is None or timestamp is None or nonce is None:
            raise WebhookVerificationError("webhook HMAC headers are required")
        if (
            not nonce
            or len(nonce) > 128
            or any(ord(character) < 33 or ord(character) == 127 for character in nonce)
        ):
            raise WebhookVerificationError("webhook nonce is invalid")
        if not timestamp.isascii() or not timestamp.isdecimal():
            raise WebhookVerificationError("webhook timestamp must be Unix seconds")
        seconds = int(timestamp)
        if abs(self._clock() - seconds) > self._timestamp_tolerance_seconds:
            raise WebhookVerificationError("webhook timestamp is outside the allowed window")
        expected = hmac.new(
            secret,
            (
                timestamp.encode("ascii")
                + b"\n"
                + source_name.encode("utf-8")
                + b"\n"
                + nonce.encode("utf-8")
                + b"\n"
                + body
            ),
            hashlib.sha256,
        ).hexdigest()
        supplied = signature.removeprefix("sha256=")
        if not hmac.compare_digest(expected, supplied):
            raise WebhookVerificationError("webhook HMAC signature does not match")


def generic_webhook_event(
    *,
    source_name: str,
    headers: Mapping[str, str],
    body: bytes,
    received_at: datetime | None = None,
) -> CloudEventEnvelope:
    _validate_source_name(source_name)
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookVerificationError("webhook payload must be UTF-8 JSON") from exc
    data: JsonObject
    if isinstance(raw, dict):
        data = cast(JsonObject, raw)
    else:
        data = {"payload": cast(JsonValue, raw)}
    normalized = {name.lower(): value.strip() for name, value in headers.items()}
    event_id = normalized.get("x-misaka-nonce") or normalized.get("x-misaka-event-id")
    if not event_id:
        event_id = hashlib.sha256(body).hexdigest()
    return CloudEventEnvelope(
        id=event_id,
        source=f"urn:misaka:webhook:{source_name}",
        type="dev.misaka.webhook.received.v1",
        subject=normalized.get("x-misaka-subject"),
        time=received_at or datetime.now(UTC),
        data=data,
        extensions={"webhooksource": source_name},
    )


def _validate_source_name(source_name: str) -> None:
    if (
        not source_name
        or len(source_name) > 128
        or any(not (character.isalnum() or character in "._-") for character in source_name)
    ):
        raise WebhookVerificationError("webhook source name is invalid")
