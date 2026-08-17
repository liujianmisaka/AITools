from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.persistence.agent_models import (
    AgentExecutionEvent,
    AgentExecutionLease,
    ArtifactMetadata,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_NAMESPACE = UUID("1ee9fac2-f57b-43d4-893a-6cae16a71c1f")


@dataclass(frozen=True, slots=True)
class EvidenceEventRegistration:
    execution_id: str
    attempt_id: str | None
    event_type: str
    provider: str
    payload: JsonObject
    provider_session_id: str | None = None
    provider_turn_id: str | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        _validate_registration(self)


@dataclass(frozen=True, slots=True)
class EvidenceEventRecord:
    event_id: str
    execution_id: str
    attempt_id: str | None
    sequence: int
    event_type: str
    provider: str
    payload: JsonObject
    provider_session_id: str | None
    provider_turn_id: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactRegistration:
    artifact_id: str
    execution_id: str | None
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str
    kind: str

    def __post_init__(self) -> None:
        _validate_artifact(self)


class EvidenceRepositoryError(RuntimeError):
    pass


class ExecutionEvidenceRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(self, registration: EvidenceEventRegistration) -> EvidenceEventRecord:
        event_id = derive_evidence_event_id(registration)
        async with self._sessions() as session, session.begin():
            execution = await session.scalar(
                select(AgentExecutionLease)
                .where(AgentExecutionLease.execution_id == registration.execution_id)
                .with_for_update()
            )
            if execution is None:
                raise EvidenceRepositoryError("agent execution does not exist")
            existing = await session.get(AgentExecutionEvent, event_id)
            if existing is not None:
                if not _matches_registration(existing, registration):
                    raise EvidenceRepositoryError(
                        "evidence event ID was reused with different immutable data"
                    )
                return _event_record(existing)
            last_sequence = await session.scalar(
                select(func.max(AgentExecutionEvent.sequence)).where(
                    AgentExecutionEvent.execution_id == registration.execution_id
                )
            )
            sequence = int(last_sequence or 0) + 1
            event = AgentExecutionEvent(
                event_id=event_id,
                execution_id=registration.execution_id,
                attempt_id=registration.attempt_id,
                sequence=sequence,
                event_type=registration.event_type,
                provider=registration.provider,
                provider_session_id=registration.provider_session_id,
                provider_turn_id=registration.provider_turn_id,
                payload=dict(registration.payload),
            )
            session.add(event)
            await session.flush()
            return _event_record(event)

    async def list_for_execution(self, execution_id: str) -> tuple[EvidenceEventRecord, ...]:
        async with self._sessions() as session:
            events = await session.scalars(
                select(AgentExecutionEvent)
                .where(AgentExecutionEvent.execution_id == execution_id)
                .order_by(AgentExecutionEvent.sequence)
            )
            return tuple(_event_record(event) for event in events)


class ArtifactRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def register(self, registration: ArtifactRegistration) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                pg_insert(ArtifactMetadata)
                .values(
                    artifact_id=registration.artifact_id,
                    execution_id=registration.execution_id,
                    relative_path=registration.relative_path,
                    sha256=registration.sha256,
                    size_bytes=registration.size_bytes,
                    media_type=registration.media_type,
                    kind=registration.kind,
                )
                .on_conflict_do_nothing()
            )
            row = await session.get(ArtifactMetadata, registration.artifact_id)
            if row is None:
                raise EvidenceRepositoryError("artifact registration was not persisted")
            immutable = (
                row.execution_id,
                row.relative_path,
                row.sha256,
                row.size_bytes,
                row.media_type,
                row.kind,
            )
            expected = (
                registration.execution_id,
                registration.relative_path,
                registration.sha256,
                registration.size_bytes,
                registration.media_type,
                registration.kind,
            )
            if immutable != expected:
                raise EvidenceRepositoryError(
                    "artifact ID was reused with different immutable metadata"
                )


def _validate_registration(registration: EvidenceEventRegistration) -> None:
    if not registration.execution_id.strip() or len(registration.execution_id) > 512:
        raise ValueError("execution ID must not be blank")
    if not registration.event_type.strip() or len(registration.event_type) > 96:
        raise ValueError("event type must not be blank")
    if not registration.provider.strip() or len(registration.provider) > 64:
        raise ValueError("provider must not be blank")
    if registration.attempt_id is not None and len(registration.attempt_id) > 128:
        raise ValueError("attempt ID exceeds the persistence limit")
    if registration.provider_session_id is not None and len(registration.provider_session_id) > 512:
        raise ValueError("provider session ID exceeds the persistence limit")
    if registration.provider_turn_id is not None and len(registration.provider_turn_id) > 512:
        raise ValueError("provider turn ID exceeds the persistence limit")
    if registration.event_id is not None:
        _canonical_uuid(registration.event_id, label="evidence event ID")


def derive_evidence_event_id(registration: EvidenceEventRegistration) -> str:
    if registration.event_id is not None:
        return registration.event_id
    material = {
        "executionId": registration.execution_id,
        "attemptId": registration.attempt_id,
        "eventType": registration.event_type,
        "provider": registration.provider,
        "providerSessionId": registration.provider_session_id,
        "providerTurnId": registration.provider_turn_id,
        "payload": registration.payload,
    }
    canonical = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid5(_EVIDENCE_NAMESPACE, canonical))


def _validate_artifact(registration: ArtifactRegistration) -> None:
    _canonical_uuid(registration.artifact_id, label="artifact ID")
    relative = PurePosixPath(registration.relative_path)
    if (
        not registration.relative_path
        or len(registration.relative_path) > 512
        or "\\" in registration.relative_path
        or relative.is_absolute()
        or ".." in relative.parts
    ):
        raise ValueError("artifact relative path must be a safe POSIX relative path")
    if not _SHA256_PATTERN.fullmatch(registration.sha256):
        raise ValueError("artifact SHA-256 must contain 64 hexadecimal characters")
    if registration.size_bytes < 0:
        raise ValueError("artifact size must not be negative")
    if (
        not registration.media_type.strip()
        or len(registration.media_type) > 255
        or not registration.kind.strip()
        or len(registration.kind) > 64
    ):
        raise ValueError("artifact media type and kind must not be blank")


def _event_record(event: AgentExecutionEvent) -> EvidenceEventRecord:
    return EvidenceEventRecord(
        event_id=event.event_id,
        execution_id=event.execution_id,
        attempt_id=event.attempt_id,
        sequence=event.sequence,
        event_type=event.event_type,
        provider=event.provider,
        payload=event.payload,
        provider_session_id=event.provider_session_id,
        provider_turn_id=event.provider_turn_id,
        occurred_at=event.occurred_at,
    )


def _matches_registration(
    event: AgentExecutionEvent,
    registration: EvidenceEventRegistration,
) -> bool:
    return (
        event.execution_id == registration.execution_id
        and event.attempt_id == registration.attempt_id
        and event.event_type == registration.event_type
        and event.provider == registration.provider
        and event.payload == registration.payload
        and event.provider_session_id == registration.provider_session_id
        and event.provider_turn_id == registration.provider_turn_id
    )


def _canonical_uuid(value: str, *, label: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError(f"{label} must be a canonical UUID")
    return canonical
