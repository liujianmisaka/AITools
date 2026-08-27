from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import quote, urlparse

import httpx

from misaka_coordinator_service.domain._serialization import ensure_text
from misaka_coordinator_service.execution.contracts import (
    DelegationSessionEvent,
    DelegationSessionSnapshot,
    SessionStreamEvent,
    SessionStreamEventKind,
    V3ActorKind,
)


class V3SessionGatewayError(RuntimeError):
    """Base error for V3 session inspection and streaming."""


class V3SessionHTTPError(V3SessionGatewayError):
    """Raised when the public V3 session endpoint rejects a request."""


class V3SessionProtocolError(V3SessionGatewayError):
    """Raised when a V3 session response or SSE frame is malformed."""


class AsyncHTTPClient(Protocol):
    async def get(
        self,
        url: str | httpx.URL,
        *,
        params: object = None,
    ) -> httpx.Response: ...

    def stream(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        params: object = None,
        headers: object = None,
    ) -> Any: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class V3SessionGatewayConfig:
    control_plane_url: str
    actor_id: str
    actor_kind: V3ActorKind = V3ActorKind.AGENT
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        normalized_url = ensure_text(self.control_plane_url, "control_plane_url").rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise V3SessionGatewayError("control_plane_url must be an absolute HTTP(S) URL")
        object.__setattr__(self, "control_plane_url", normalized_url)
        object.__setattr__(self, "actor_id", ensure_text(self.actor_id, "actor_id"))
        if isinstance(self.request_timeout_seconds, bool) or self.request_timeout_seconds <= 0:
            raise V3SessionGatewayError("request_timeout_seconds must be greater than zero")


class V3SessionGateway:
    def __init__(
        self,
        *,
        config: V3SessionGatewayConfig,
        client: AsyncHTTPClient | httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = cast(
            AsyncHTTPClient,
            client or httpx.AsyncClient(timeout=config.request_timeout_seconds),
        )
        self._owns_client = client is None

    async def get_session(self, delegation_id: str) -> DelegationSessionSnapshot:
        normalized_delegation_id = ensure_text(delegation_id, "delegation_id")
        response = await self._client.get(
            self._url(normalized_delegation_id, "session"),
            params=self._actor_params(),
        )
        snapshot = _parse_session_snapshot(self._response_json(response, "get session"))
        if snapshot.delegation.delegation_id != normalized_delegation_id:
            raise V3SessionProtocolError(
                "session snapshot delegation_id does not match the request"
            )
        return snapshot

    async def list_events(
        self,
        delegation_id: str,
        *,
        next_sequence: int = 1,
    ) -> tuple[DelegationSessionEvent, ...]:
        if isinstance(next_sequence, bool) or next_sequence < 1:
            raise V3SessionGatewayError("next_sequence must be positive")
        normalized_delegation_id = ensure_text(delegation_id, "delegation_id")
        response = await self._client.get(
            self._url(normalized_delegation_id, "session/events"),
            params={**self._actor_params(), "next_sequence": next_sequence},
        )
        payload = self._response_json(response, "list session events")
        if not isinstance(payload, list):
            raise V3SessionProtocolError("list session events must return an array")
        events = tuple(_parse_session_event(item) for item in cast(list[object], payload))
        if any(event.delegation_id != normalized_delegation_id for event in events):
            raise V3SessionProtocolError("session event delegation_id does not match the request")
        _ensure_monotonic_sequences(events, next_sequence)
        return events

    async def stream_events(
        self,
        delegation_id: str,
        *,
        next_sequence: int = 1,
    ) -> AsyncIterator[SessionStreamEvent]:
        if isinstance(next_sequence, bool) or next_sequence < 1:
            raise V3SessionGatewayError("next_sequence must be positive")
        normalized_delegation_id = ensure_text(delegation_id, "delegation_id")
        params = {**self._actor_params(), "next_sequence": next_sequence}
        headers = {"Accept": "text/event-stream"}
        async with self._client.stream(
            "GET",
            self._url(normalized_delegation_id, "session/stream"),
            params=params,
            headers=headers,
        ) as response:
            self._ensure_success(response, "stream session events")
            async for frame in _iter_sse_frames(response):
                yield _decode_sse_frame(frame, normalized_delegation_id)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, delegation_id: str, suffix: str) -> str:
        return (
            f"{self._config.control_plane_url}/delegations/"
            f"{quote(ensure_text(delegation_id, 'delegation_id'), safe='')}"
            f"/{suffix}"
        )

    def _actor_params(self) -> dict[str, str]:
        return {"actor_id": self._config.actor_id, "actor_kind": self._config.actor_kind.value}

    @staticmethod
    def _ensure_success(response: httpx.Response, operation: str) -> None:
        if response.is_success:
            return
        detail = response.text[:500]
        raise V3SessionHTTPError(
            f"V3 {operation} failed with HTTP {response.status_code}: {detail}"
        )

    @classmethod
    def _response_json(cls, response: httpx.Response, operation: str) -> object:
        cls._ensure_success(response, operation)
        try:
            return cast(object, response.json())
        except ValueError as error:
            raise V3SessionProtocolError(f"V3 {operation} returned invalid JSON") from error


@dataclass(frozen=True, slots=True)
class _SSEFrame:
    event: str | None
    event_id: str | None
    data: str


async def _iter_sse_frames(response: httpx.Response) -> AsyncIterator[_SSEFrame]:
    event: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield _SSEFrame(event=event, event_id=event_id, data="\n".join(data_lines))
            event = None
            event_id = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield _SSEFrame(event=event, event_id=event_id, data="\n".join(data_lines))


def _decode_sse_frame(frame: _SSEFrame, delegation_id: str) -> SessionStreamEvent:
    if frame.event is None:
        raise V3SessionProtocolError("session SSE frame has no event name")
    try:
        payload = cast(object, json.loads(frame.data))
    except json.JSONDecodeError as error:
        raise V3SessionProtocolError("session SSE frame contains invalid JSON") from error
    if frame.event == SessionStreamEventKind.SNAPSHOT.value:
        snapshot = _parse_session_snapshot(payload)
        if snapshot.delegation.delegation_id != delegation_id:
            raise V3SessionProtocolError(
                "session snapshot delegation_id does not match the request"
            )
        return SessionStreamEvent(
            kind=SessionStreamEventKind.SNAPSHOT,
            event_id=frame.event_id,
            snapshot=snapshot,
        )
    if frame.event == SessionStreamEventKind.EVENT.value:
        event = _parse_session_event(payload)
        if event.delegation_id != delegation_id:
            raise V3SessionProtocolError("session event delegation_id does not match the request")
        return SessionStreamEvent(
            kind=SessionStreamEventKind.EVENT,
            event_id=frame.event_id,
            session_event=event,
        )
    if frame.event == SessionStreamEventKind.END.value:
        data = _object(payload, "session end")
        raw_delegation_id = data.get("delegation_id")
        if raw_delegation_id != delegation_id:
            raise V3SessionProtocolError("session end delegation_id does not match the request")
        next_sequence = data.get("next_sequence")
        if (
            isinstance(next_sequence, bool)
            or not isinstance(next_sequence, int)
            or next_sequence < 1
        ):
            raise V3SessionProtocolError("session end next_sequence must be positive")
        return SessionStreamEvent(
            kind=SessionStreamEventKind.END,
            event_id=frame.event_id,
            next_sequence=next_sequence,
        )
    raise V3SessionProtocolError(f"unsupported session SSE event {frame.event!r}")


def _object(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise V3SessionProtocolError(f"{field_name} must be an object")
    raw = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        raise V3SessionProtocolError(f"{field_name} keys must be strings")
    return cast(dict[str, object], raw)


def _parse_session_snapshot(value: object) -> DelegationSessionSnapshot:
    try:
        return DelegationSessionSnapshot.from_object(value)
    except ValueError as error:
        raise V3SessionProtocolError(
            f"session snapshot violates the V3 contract: {error}"
        ) from error


def _parse_session_event(value: object) -> DelegationSessionEvent:
    try:
        return DelegationSessionEvent.from_object(value)
    except ValueError as error:
        raise V3SessionProtocolError(f"session event violates the V3 contract: {error}") from error


def _ensure_monotonic_sequences(
    events: tuple[DelegationSessionEvent, ...],
    next_sequence: int,
) -> None:
    previous = next_sequence - 1
    for event in events:
        if event.sequence <= previous:
            raise V3SessionProtocolError("session event sequences must be strictly increasing")
        previous = event.sequence
