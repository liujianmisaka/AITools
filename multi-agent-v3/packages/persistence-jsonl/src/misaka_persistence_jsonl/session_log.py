from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TypeVar, cast

from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import (
    CURRENT_DURABLE_FORMAT_VERSION,
    DurableConflict,
    DurableCorruption,
    DurableEvent,
    DurableNotFound,
    DurableProjection,
    ProjectionReplay,
    SessionHeader,
    SessionInspection,
)

from misaka_persistence_jsonl.event_log import JsonlEventLog

StateT = TypeVar("StateT")


class JsonlSessionLog:
    """Session headers and append-only facts backed by one JSONL event log."""

    _HEADER_PREFIX = "session.header:"
    _FACT_PREFIX = "session.facts:"
    _HEADER_EVENT_ID = "session-created"
    _HEADER_EVENT_TYPE = "session.created"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._lock = asyncio.Lock()
        self._headers: dict[str, SessionHeader] = {}
        self._opened = False

    async def open(self) -> None:
        async with self._lock:
            if self._opened:
                return
            headers: dict[str, SessionHeader] = {}
            try:
                fact_sessions: set[str] = set()
                for event in await self._log.all_events():
                    if event.stream_id.startswith(self._HEADER_PREFIX):
                        session_id = event.stream_id.removeprefix(self._HEADER_PREFIX)
                        if not session_id.strip():
                            raise DurableCorruption(
                                "session.id_empty",
                                "session header stream has an empty session id",
                            )
                        if event.sequence != 1 or event.event_id != self._HEADER_EVENT_ID:
                            raise DurableCorruption(
                                "session.header_sequence_invalid",
                                f"session {session_id} has an invalid header sequence",
                            )
                        if event.event_type != self._HEADER_EVENT_TYPE:
                            raise DurableCorruption(
                                "session.header_type_invalid",
                                f"session {session_id} has an invalid header fact",
                            )
                        if session_id in headers:
                            raise DurableCorruption(
                                "session.duplicate_header",
                                f"session {session_id} has duplicate headers",
                            )
                        header = _decode_header(event.payload)
                        if header.session_id != session_id:
                            raise DurableCorruption(
                                "session.id_mismatch",
                                f"session header does not match stream {session_id}",
                            )
                        headers[session_id] = header
                    elif event.stream_id.startswith(self._FACT_PREFIX):
                        session_id = event.stream_id.removeprefix(self._FACT_PREFIX)
                        if not session_id.strip():
                            raise DurableCorruption(
                                "session.id_empty",
                                "session fact stream has an empty session id",
                            )
                        fact_sessions.add(session_id)
                missing = fact_sessions - headers.keys()
                if missing:
                    raise DurableCorruption(
                        "session.header_missing",
                        f"session facts exist without headers: {sorted(missing)!r}",
                    )
            except DurableCorruption:
                raise
            except Exception as exc:
                raise DurableCorruption(
                    "session.invalid_fact",
                    "session JSONL facts have an invalid shape",
                ) from exc
            self._headers = headers
            self._opened = True

    async def create(self, header: SessionHeader) -> SessionHeader:
        await self.open()
        if header.format_version != CURRENT_DURABLE_FORMAT_VERSION:
            raise DurableConflict(
                "session.unsupported_format",
                f"unsupported session format version {header.format_version}",
            )
        async with self._lock:
            existing = self._headers.get(header.session_id)
            if existing is not None:
                if existing != header:
                    raise DurableConflict(
                        "session.header_conflict",
                        f"session {header.session_id} already has a different header",
                    )
                return existing
            await self._log.append(
                self._header_stream(header.session_id),
                self._HEADER_EVENT_ID,
                self._HEADER_EVENT_TYPE,
                _encode_header(header),
                occurred_at=header.created_at,
            )
            self._headers[header.session_id] = header
            return header

    async def get(self, session_id: str) -> SessionHeader:
        await self.open()
        session_id = _validate_id(session_id, "session_id")
        try:
            return self._headers[session_id]
        except KeyError as exc:
            raise DurableNotFound(
                "session.not_found", f"session {session_id} was not found"
            ) from exc

    async def list(self) -> tuple[SessionHeader, ...]:
        await self.open()
        return tuple(self._headers[session_id] for session_id in sorted(self._headers))

    async def append(
        self,
        session_id: str,
        fact_id: str,
        fact_type: str,
        payload: JsonObject,
        *,
        schema_version: int = CURRENT_DURABLE_FORMAT_VERSION,
        occurred_at: datetime | None = None,
    ) -> DurableEvent:
        await self.open()
        session_id = _validate_id(session_id, "session_id")
        _validate_id(fact_id, "fact_id")
        _validate_id(fact_type, "fact_type")
        if schema_version != CURRENT_DURABLE_FORMAT_VERSION:
            raise DurableConflict(
                "session.unsupported_schema",
                f"unsupported session fact schema version {schema_version}",
            )
        async with self._lock:
            if session_id not in self._headers:
                raise DurableNotFound("session.not_found", f"session {session_id} was not found")
            return await self._log.append(
                self._fact_stream(session_id),
                fact_id,
                fact_type,
                payload,
                schema_version=schema_version,
                occurred_at=occurred_at,
            )

    async def read(self, session_id: str, *, start_sequence: int = 1) -> tuple[DurableEvent, ...]:
        await self.get(session_id)
        return await self._log.read(self._fact_stream(session_id), start_sequence=start_sequence)

    async def inspect(self, session_id: str) -> SessionInspection:
        header = await self.get(session_id)
        facts = await self._log.read(self._fact_stream(session_id))
        last_sequence = facts[-1].sequence if facts else 0
        return SessionInspection(
            header=header,
            last_sequence=last_sequence,
            fact_count=len(facts),
        )

    async def replay[StateT](
        self,
        session_id: str,
        projection: DurableProjection[StateT],
        *,
        start_sequence: int = 1,
        reset: bool = True,
        projection_version: int = 1,
    ) -> ProjectionReplay[StateT]:
        await self.get(session_id)
        return await self._log.replay_with_checkpoint(
            self._fact_stream(session_id),
            projection,
            start_sequence=start_sequence,
            reset=reset,
            projection_version=projection_version,
        )

    def events(self, session_id: str, *, start_sequence: int = 1) -> AsyncIterator[DurableEvent]:
        return self._events(session_id, start_sequence=start_sequence)

    async def _events(self, session_id: str, *, start_sequence: int) -> AsyncIterator[DurableEvent]:
        for event in await self.read(session_id, start_sequence=start_sequence):
            yield event

    @classmethod
    def _header_stream(cls, session_id: str) -> str:
        return f"{cls._HEADER_PREFIX}{_validate_id(session_id, 'session_id')}"

    @classmethod
    def _fact_stream(cls, session_id: str) -> str:
        return f"{cls._FACT_PREFIX}{_validate_id(session_id, 'session_id')}"


def _encode_header(header: SessionHeader) -> JsonObject:
    return {
        "session_id": header.session_id,
        "owner_id": header.owner_id,
        "scope_id": header.scope_id,
        "composition_id": header.composition_id,
        "parent_session_id": header.parent_session_id,
        "metadata": header.metadata,
        "format_version": header.format_version,
        "created_at": header.created_at.isoformat(),
    }


def _decode_header(payload: JsonObject) -> SessionHeader:
    format_version = _required_int(payload, "format_version")
    if format_version != CURRENT_DURABLE_FORMAT_VERSION:
        raise DurableCorruption(
            "session.unsupported_format",
            f"unsupported session format version {format_version}",
        )
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("session metadata must be an object")
    return SessionHeader(
        session_id=_required_string(payload, "session_id"),
        owner_id=_required_string(payload, "owner_id"),
        scope_id=_required_string(payload, "scope_id"),
        composition_id=_required_string(payload, "composition_id"),
        parent_session_id=_optional_string(payload.get("parent_session_id")),
        metadata=cast(JsonObject, metadata),
        format_version=format_version,
        created_at=datetime.fromisoformat(_required_string(payload, "created_at")),
    )


def _validate_id(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


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
