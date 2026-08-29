from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from agent_framework import AgentSession

from misaka_coordinator_service.domain import CoordinatorSession
from misaka_coordinator_service.domain._serialization import ensure_optional_text, ensure_text

SESSION_RECORD_SCHEMA_VERSION = 2
_LEGACY_SESSION_RECORD_SCHEMA_VERSION = 1


def _empty_record_payload() -> Mapping[str, object]:
    return {}


class CoordinatorSessionStoreError(RuntimeError):
    """Base error for Coordinator session persistence."""


class SessionRecordConflictError(CoordinatorSessionStoreError):
    """Raised when a compare-and-set save observes a newer record."""


class SessionRecordCorruptedError(CoordinatorSessionStoreError):
    """Raised when a JSONL record cannot be safely replayed."""


@dataclass(frozen=True, slots=True)
class PendingEventActivation:
    delegation_id: str
    sequence: int
    activation_id: str
    event_type: str
    event_id: str
    external_event_id: str | None
    source_event_kind: str | None
    source_event_status: str | None
    source_event_payload: Mapping[str, object] = field(default_factory=_empty_record_payload)

    def __post_init__(self) -> None:
        for field_name in ("delegation_id", "activation_id", "event_type", "event_id"):
            object.__setattr__(self, field_name, ensure_text(getattr(self, field_name), field_name))
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise CoordinatorSessionStoreError("pending event activation sequence must be positive")
        for field_name in (
            "external_event_id",
            "source_event_kind",
            "source_event_status",
        ):
            object.__setattr__(
                self,
                field_name,
                ensure_optional_text(getattr(self, field_name), field_name),
            )
        raw_payload = cast(Mapping[object, object], self.source_event_payload)
        if any(not isinstance(key, str) for key in raw_payload):
            raise CoordinatorSessionStoreError(
                "pending event activation payload keys must be strings"
            )
        object.__setattr__(
            self,
            "source_event_payload",
            dict(cast(Mapping[str, object], raw_payload)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "delegation_id": self.delegation_id,
            "sequence": self.sequence,
            "activation_id": self.activation_id,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "external_event_id": self.external_event_id,
            "source_event_kind": self.source_event_kind,
            "source_event_status": self.source_event_status,
            "source_event_payload": dict(self.source_event_payload),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PendingEventActivation:
        sequence = value.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise SessionRecordCorruptedError(
                "pending event activation sequence must be an integer"
            )
        raw_payload = value.get("source_event_payload", {})
        if not isinstance(raw_payload, Mapping):
            raise SessionRecordCorruptedError("pending event activation payload must be an object")
        payload = cast(Mapping[object, object], raw_payload)
        if any(not isinstance(key, str) for key in payload):
            raise SessionRecordCorruptedError(
                "pending event activation payload keys must be strings"
            )
        try:
            return cls(
                delegation_id=ensure_text(value.get("delegation_id"), "delegation_id"),
                sequence=sequence,
                activation_id=ensure_text(value.get("activation_id"), "activation_id"),
                event_type=ensure_text(value.get("event_type"), "event_type"),
                event_id=ensure_text(value.get("event_id"), "event_id"),
                external_event_id=_optional_record_text(value, "external_event_id"),
                source_event_kind=_optional_record_text(value, "source_event_kind"),
                source_event_status=_optional_record_text(value, "source_event_status"),
                source_event_payload=cast(Mapping[str, object], payload),
            )
        except (TypeError, ValueError, CoordinatorSessionStoreError) as error:
            raise SessionRecordCorruptedError("pending_event_activation is invalid") from error


@dataclass(frozen=True, slots=True)
class CoordinatorSessionRecord:
    coordinator_session: CoordinatorSession
    agent_session: AgentSession
    version: int = 0
    working_directory: str | None = None
    pending_event_activation: PendingEventActivation | None = None

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or self.version < 0:
            raise CoordinatorSessionStoreError("session record version must not be negative")
        object.__setattr__(
            self,
            "working_directory",
            ensure_optional_text(self.working_directory, "working_directory"),
        )

    @property
    def session_id(self) -> str:
        return self.coordinator_session.session_id

    @property
    def revision(self) -> int:
        return self.coordinator_session.revision

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SESSION_RECORD_SCHEMA_VERSION,
            "session_id": self.session_id,
            "version": self.version,
            "revision": self.revision,
            "coordinator_session": self.coordinator_session.to_dict(),
            "agent_session": self.agent_session.to_dict(),
            "working_directory": self.working_directory,
            "pending_event_activation": (
                None
                if self.pending_event_activation is None
                else self.pending_event_activation.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CoordinatorSessionRecord:
        schema_version = value.get("schema_version")
        if schema_version not in {
            _LEGACY_SESSION_RECORD_SCHEMA_VERSION,
            SESSION_RECORD_SCHEMA_VERSION,
        }:
            raise SessionRecordCorruptedError("unsupported coordinator session record version")
        session_id = value.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise SessionRecordCorruptedError("session record session_id must be non-empty")
        revision = value.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise SessionRecordCorruptedError("session record revision must be an integer")
        version = value.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise SessionRecordCorruptedError("session record version must be positive")
        coordinator_payload = value.get("coordinator_session")
        if not isinstance(coordinator_payload, dict):
            raise SessionRecordCorruptedError("coordinator_session must be an object")
        raw_coordinator = cast(dict[object, object], coordinator_payload)
        if any(not isinstance(key, str) for key in raw_coordinator):
            raise SessionRecordCorruptedError("coordinator_session keys must be strings")
        try:
            coordinator_session = CoordinatorSession.from_dict(
                cast(dict[str, object], raw_coordinator)
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SessionRecordCorruptedError("coordinator_session is invalid") from error
        if coordinator_session.session_id != session_id or coordinator_session.revision != revision:
            raise SessionRecordCorruptedError("session record identity or revision is inconsistent")
        agent_payload = value.get("agent_session")
        if not isinstance(agent_payload, dict):
            raise SessionRecordCorruptedError("agent_session must be an object")
        raw_agent = cast(dict[object, object], agent_payload)
        if any(not isinstance(key, str) for key in raw_agent):
            raise SessionRecordCorruptedError("agent_session keys must be strings")
        try:
            agent_session = AgentSession.from_dict(cast(dict[str, Any], raw_agent))
        except (KeyError, TypeError, ValueError) as error:
            raise SessionRecordCorruptedError("agent_session is invalid") from error
        working_directory = value.get("working_directory")
        if schema_version == _LEGACY_SESSION_RECORD_SCHEMA_VERSION:
            working_directory = None
        elif working_directory is not None and not isinstance(working_directory, str):
            raise SessionRecordCorruptedError("working_directory must be a string or null")
        pending_event_activation = None
        if schema_version == SESSION_RECORD_SCHEMA_VERSION:
            raw_pending = value.get("pending_event_activation")
            if raw_pending is not None:
                if not isinstance(raw_pending, Mapping):
                    raise SessionRecordCorruptedError(
                        "pending_event_activation must be an object or null"
                    )
                raw_pending_mapping = cast(Mapping[object, object], raw_pending)
                if any(not isinstance(key, str) for key in raw_pending_mapping):
                    raise SessionRecordCorruptedError(
                        "pending_event_activation keys must be strings"
                    )
                pending_event_activation = PendingEventActivation.from_dict(
                    cast(Mapping[str, object], raw_pending_mapping)
                )
        return cls(
            coordinator_session=coordinator_session,
            agent_session=agent_session,
            version=version,
            working_directory=working_directory,
            pending_event_activation=pending_event_activation,
        )


class JsonlCoordinatorSessionStore:
    """Append-only local persistence for Coordinator and MAF session state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()

    def load(self, session_id: str) -> CoordinatorSessionRecord | None:
        normalized = ensure_text(session_id, "session_id")
        with self._lock:
            return self._read_latest().get(normalized)

    def list_session_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._read_latest()))

    def list_records(self) -> tuple[CoordinatorSessionRecord, ...]:
        with self._lock:
            latest = self._read_latest()
            return tuple(latest[session_id] for session_id in sorted(latest))

    def save(
        self,
        record: CoordinatorSessionRecord,
        *,
        expected_version: int,
    ) -> CoordinatorSessionRecord:
        if isinstance(expected_version, bool) or expected_version < 0:
            raise CoordinatorSessionStoreError("expected_version must not be negative")
        if record.coordinator_session.cognitive_session_id != record.agent_session.session_id:
            raise CoordinatorSessionStoreError(
                "Coordinator cognitive_session_id and Agent session_id must match"
            )
        with self._lock:
            latest = self._read_latest()
            current = latest.get(record.session_id)
            current_version = 0 if current is None else current.version
            if current_version != expected_version:
                raise SessionRecordConflictError(
                    f"session {record.session_id} expected version {expected_version}, "
                    f"current version is {current_version}"
                )
            if current is not None and record.revision < current.revision:
                raise SessionRecordConflictError("domain revision cannot move backwards")
            candidate = CoordinatorSessionRecord(
                coordinator_session=record.coordinator_session,
                agent_session=record.agent_session,
                version=current_version + 1,
                working_directory=record.working_directory,
                pending_event_activation=record.pending_event_activation,
            )
            if current is not None and (
                current.coordinator_session == candidate.coordinator_session
                and current.agent_session.to_dict() == candidate.agent_session.to_dict()
                and current.working_directory == candidate.working_directory
                and current.pending_event_activation == candidate.pending_event_activation
            ):
                return current
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(candidate.to_dict(), ensure_ascii=False, separators=(",", ":"))
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as error:
                raise CoordinatorSessionStoreError(
                    "failed to append coordinator session record"
                ) from error
            return candidate

    def close(self) -> None:
        return None

    def _read_latest(self) -> dict[str, CoordinatorSessionRecord]:
        if not self.path.exists():
            return {}
        latest: dict[str, CoordinatorSessionRecord] = {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        decoded = cast(object, json.loads(line))
                    except json.JSONDecodeError as error:
                        raise SessionRecordCorruptedError(
                            f"session store line {line_number} is not valid JSON"
                        ) from error
                    if not isinstance(decoded, dict):
                        raise SessionRecordCorruptedError(
                            f"session store line {line_number} must be an object"
                        )
                    raw = cast(dict[object, object], decoded)
                    if any(not isinstance(key, str) for key in raw):
                        raise SessionRecordCorruptedError(
                            f"session store line {line_number} keys must be strings"
                        )
                    record = CoordinatorSessionRecord.from_dict(cast(dict[str, object], raw))
                    previous = latest.get(record.session_id)
                    if previous is not None and record.version != previous.version + 1:
                        raise SessionRecordCorruptedError(
                            f"session store line {line_number} has a non-contiguous version"
                        )
                    if previous is not None and record.revision < previous.revision:
                        raise SessionRecordCorruptedError(
                            f"session store line {line_number} moves revision backwards"
                        )
                    latest[record.session_id] = record
        except OSError as error:
            raise CoordinatorSessionStoreError(
                "failed to read coordinator session store"
            ) from error
        return latest


def _optional_record_text(value: Mapping[str, object], field_name: str) -> str | None:
    item = value.get(field_name)
    if item is None:
        return None
    if not isinstance(item, str):
        raise SessionRecordCorruptedError(f"{field_name} must be a string or null")
    return item
