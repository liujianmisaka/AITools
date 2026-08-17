from __future__ import annotations

from typing import Protocol

from multi_agent_v2.packages.artifacts.store import LocalArtifactStore, StoredArtifact, redact_json
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.persistence.evidence_repository import (
    ArtifactRegistration,
    ArtifactRepository,
    EvidenceEventRecord,
    EvidenceEventRegistration,
    ExecutionEvidenceRepository,
    derive_evidence_event_id,
)


class EvidenceRecorder(Protocol):
    async def record(
        self,
        *,
        execution_id: str,
        attempt_id: str | None,
        event_type: str,
        provider: str,
        payload: JsonObject,
        provider_session_id: str | None = None,
        provider_turn_id: str | None = None,
        preserve_payload: bool = False,
    ) -> EvidenceEventRecord: ...


class ExecutionEvidenceService:
    def __init__(
        self,
        *,
        events: ExecutionEvidenceRepository,
        artifacts: ArtifactRepository,
        store: LocalArtifactStore,
    ) -> None:
        self._events = events
        self._artifacts = artifacts
        self._store = store

    async def record(
        self,
        *,
        execution_id: str,
        attempt_id: str | None,
        event_type: str,
        provider: str,
        payload: JsonObject,
        provider_session_id: str | None = None,
        provider_turn_id: str | None = None,
        preserve_payload: bool = False,
    ) -> EvidenceEventRecord:
        safe_payload = redact_json(payload)
        assert isinstance(safe_payload, dict)
        registration = EvidenceEventRegistration(
            execution_id=execution_id,
            attempt_id=attempt_id,
            event_type=event_type,
            provider=provider,
            payload=safe_payload,
            provider_session_id=provider_session_id,
            provider_turn_id=provider_turn_id,
        )
        event_id = derive_evidence_event_id(registration)
        artifact: StoredArtifact | None = None
        if preserve_payload:
            artifact = await self._store.put_json(
                safe_payload,
                kind=f"agent-event-{event_type}",
                artifact_id=event_id,
            )
            try:
                await self._artifacts.register(
                    ArtifactRegistration(
                        artifact_id=artifact.artifact_id,
                        execution_id=execution_id,
                        relative_path=artifact.relative_path,
                        sha256=artifact.sha256,
                        size_bytes=artifact.size_bytes,
                        media_type=artifact.media_type,
                        kind=artifact.kind,
                    )
                )
            except BaseException:
                await self._store.delete(artifact)
                raise
            safe_payload = {
                "rawArtifactId": artifact.artifact_id,
                "kind": payload.get("kind"),
                "sequence": payload.get("sequence"),
                "summary": payload.get("summary"),
            }
        return await self._events.append(
            EvidenceEventRegistration(
                execution_id=registration.execution_id,
                attempt_id=registration.attempt_id,
                event_type=registration.event_type,
                provider=registration.provider,
                payload=safe_payload,
                provider_session_id=registration.provider_session_id,
                provider_turn_id=registration.provider_turn_id,
                event_id=event_id,
            )
        )
