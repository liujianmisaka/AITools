from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast


class ControlPlaneStateRecoveryError(RuntimeError):
    """Raised when a Control Plane state file cannot be safely inspected or repaired."""


@dataclass(frozen=True, slots=True)
class StateConflict:
    """One idempotency key that appears in more than one delegation stream."""

    idempotency_key: str
    streams: tuple[str, ...]
    canonical_stream: str | None
    orphan_streams: tuple[str, ...]
    reason: str

    @property
    def repairable(self) -> bool:
        return self.canonical_stream is not None and bool(self.orphan_streams)

    def to_payload(self) -> dict[str, object]:
        return {
            "idempotency_key": self.idempotency_key,
            "streams": list(self.streams),
            "canonical_stream": self.canonical_stream,
            "orphan_streams": list(self.orphan_streams),
            "repairable": self.repairable,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneStateReport:
    """Inspection result for a Control Plane JSONL state file."""

    path: Path
    event_count: int
    stream_count: int
    conflicts: tuple[StateConflict, ...]
    backup_path: Path | None = None
    removed_streams: tuple[str, ...] = ()

    @property
    def repairable(self) -> bool:
        return bool(self.conflicts) and all(conflict.repairable for conflict in self.conflicts)

    def to_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "event_count": self.event_count,
            "stream_count": self.stream_count,
            "conflicts": [conflict.to_payload() for conflict in self.conflicts],
            "repairable": self.repairable,
            "backup_path": str(self.backup_path) if self.backup_path is not None else None,
            "removed_streams": list(self.removed_streams),
        }


def inspect_control_plane_state(path: str | Path) -> ControlPlaneStateReport:
    """Inspect delegation streams and identify safe idempotency-key repairs."""

    state_path = Path(path).resolve()
    records = _read_records(state_path)
    streams = _group_streams(records)
    creations_by_key: dict[str, list[str]] = defaultdict(list)
    for stream_id, stream_records in streams.items():
        creation = _creation_record(stream_id, stream_records)
        if creation is not None:
            idempotency_key = _creation_idempotency_key(stream_id, creation)
            creations_by_key[idempotency_key].append(stream_id)

    conflicts = tuple(
        _classify_conflict(key, tuple(stream_ids), streams)
        for key, stream_ids in sorted(creations_by_key.items())
        if len(stream_ids) > 1
    )
    return ControlPlaneStateReport(
        path=state_path,
        event_count=len(records),
        stream_count=len(streams),
        conflicts=conflicts,
    )


def repair_control_plane_state(
    path: str | Path,
    *,
    backup_path: str | Path | None = None,
) -> ControlPlaneStateReport:
    """Repair only unambiguous creation-only orphan streams.

    The original file is copied before replacement and retained as an audit/rollback
    artifact. Ambiguous conflicts fail closed without changing the state file.
    """

    state_path = Path(path).resolve()
    report = inspect_control_plane_state(state_path)
    if not report.conflicts:
        return report
    if not report.repairable:
        raise ControlPlaneStateRecoveryError(_format_unrepairable_conflicts(report.conflicts))

    orphan_streams = tuple(
        stream_id for conflict in report.conflicts for stream_id in conflict.orphan_streams
    )
    resolved_backup_path = _resolve_backup_path(state_path, backup_path)
    try:
        shutil.copy2(state_path, resolved_backup_path)
    except OSError as exc:
        if resolved_backup_path.exists():
            resolved_backup_path.unlink()
        raise ControlPlaneStateRecoveryError("failed to back up Control Plane state") from exc

    try:
        _replace_without_streams(state_path, set(orphan_streams))
        repaired = inspect_control_plane_state(state_path)
        if repaired.conflicts:
            raise ControlPlaneStateRecoveryError(
                "state still contains idempotency conflicts after repair"
            )
    except Exception as exc:
        try:
            shutil.copy2(resolved_backup_path, state_path)
        except OSError as restore_exc:
            raise ControlPlaneStateRecoveryError(
                f"repair failed and original state could not be restored: {restore_exc}"
            ) from exc
        if isinstance(exc, ControlPlaneStateRecoveryError):
            raise
        raise ControlPlaneStateRecoveryError("failed to repair Control Plane state") from exc

    return ControlPlaneStateReport(
        path=repaired.path,
        event_count=repaired.event_count,
        stream_count=repaired.stream_count,
        conflicts=repaired.conflicts,
        backup_path=resolved_backup_path,
        removed_streams=orphan_streams,
    )


def _read_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise ControlPlaneStateRecoveryError(f"state file does not exist: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ControlPlaneStateRecoveryError(f"failed to read state file: {path}") from exc

    records: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ControlPlaneStateRecoveryError(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(decoded, dict):
            raise ControlPlaneStateRecoveryError(
                f"JSONL record must be an object at {path}:{line_number}"
            )
        records.append(cast(dict[str, object], decoded))
    return records


def _group_streams(records: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    streams: dict[str, list[dict[str, object]]] = {}
    event_ids: set[tuple[str, str]] = set()
    for index, record in enumerate(records, start=1):
        stream_id = _required_string(record, "stream_id", index)
        sequence = _required_int(record, "sequence", index)
        event_id = _required_string(record, "event_id", index)
        event_type = _required_string(record, "event_type", index)
        _required_object(record, "payload", index)
        if not stream_id.startswith("delegation:"):
            continue
        stream = streams.setdefault(stream_id, [])
        expected_sequence = len(stream) + 1
        if sequence != expected_sequence:
            raise ControlPlaneStateRecoveryError(
                f"stream {stream_id} expected sequence {expected_sequence}, got {sequence}"
            )
        event_key = (stream_id, event_id)
        if event_key in event_ids:
            raise ControlPlaneStateRecoveryError(
                f"stream {stream_id} contains duplicate event id {event_id}"
            )
        event_ids.add(event_key)
        if event_type == "delegation.created":
            _validate_creation_record(stream_id, record, index)
        stream.append(record)
    for stream_id, stream in streams.items():
        if _creation_record(stream_id, stream) is None:
            raise ControlPlaneStateRecoveryError(
                f"delegation stream {stream_id} has no creation fact"
            )
        if stream[0].get("event_type") != "delegation.created":
            raise ControlPlaneStateRecoveryError(
                f"delegation stream {stream_id} does not start with a creation fact"
            )
    return streams


def _creation_record(stream_id: str, records: list[dict[str, object]]) -> dict[str, object] | None:
    creations = [record for record in records if record.get("event_type") == "delegation.created"]
    if len(creations) > 1:
        raise ControlPlaneStateRecoveryError(
            f"delegation stream {stream_id} has duplicate creation facts"
        )
    return creations[0] if creations else None


def _creation_idempotency_key(stream_id: str, record: dict[str, object]) -> str:
    payload = _required_object(record, "payload", 0)
    request = _required_object(payload, "request", 0)
    key = request.get("idempotency_key")
    if not isinstance(key, str) or not key.strip():
        raise ControlPlaneStateRecoveryError(
            f"delegation stream {stream_id} has an invalid idempotency key"
        )
    return key


def _classify_conflict(
    idempotency_key: str,
    streams: tuple[str, ...],
    grouped: dict[str, list[dict[str, object]]],
) -> StateConflict:
    terminal_types = {"delegation.finalized", "delegation.reconciliation_resolved"}
    terminal_streams = tuple(
        stream_id
        for stream_id in streams
        if any(record.get("event_type") in terminal_types for record in grouped[stream_id])
    )
    creation_only = tuple(
        stream_id
        for stream_id in streams
        if len(grouped[stream_id]) == 1
        and grouped[stream_id][0].get("event_type") == "delegation.created"
    )
    if len(terminal_streams) == 1 and len(creation_only) == len(streams) - 1:
        return StateConflict(
            idempotency_key=idempotency_key,
            streams=streams,
            canonical_stream=terminal_streams[0],
            orphan_streams=creation_only,
            reason="one terminal stream and creation-only orphan stream(s)",
        )
    return StateConflict(
        idempotency_key=idempotency_key,
        streams=streams,
        canonical_stream=None,
        orphan_streams=(),
        reason=(
            "ambiguous duplicate streams; automatic repair requires exactly one terminal"
            " stream and all other streams to contain only delegation.created"
        ),
    )


def _validate_creation_record(stream_id: str, record: dict[str, object], line_number: int) -> None:
    payload = _required_object(record, "payload", line_number)
    request = _required_object(payload, "request", line_number)
    delegation_id = request.get("delegation_id")
    expected_id = stream_id.removeprefix("delegation:")
    if delegation_id != expected_id:
        raise ControlPlaneStateRecoveryError(
            f"delegation creation fact does not match stream {stream_id}"
        )
    _creation_idempotency_key(stream_id, record)
    ref = _required_object(payload, "ref", line_number)
    if ref.get("delegation_id") != expected_id:
        raise ControlPlaneStateRecoveryError(f"delegation ref does not match stream {stream_id}")


def _required_string(record: dict[str, object], name: str, line_number: int) -> str:
    value = record.get(name)
    if not isinstance(value, str) or not value.strip():
        location = f" at line {line_number}" if line_number else ""
        raise ControlPlaneStateRecoveryError(f"{name} must be a non-empty string{location}")
    return value


def _required_int(record: dict[str, object], name: str, line_number: int) -> int:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlPlaneStateRecoveryError(f"{name} must be an integer at line {line_number}")
    return value


def _required_object(record: dict[str, object], name: str, line_number: int) -> dict[str, object]:
    value = record.get(name)
    if not isinstance(value, dict):
        raise ControlPlaneStateRecoveryError(f"{name} must be an object at line {line_number}")
    return cast(dict[str, object], value)


def _resolve_backup_path(path: Path, backup_path: str | Path | None) -> Path:
    if backup_path is not None:
        resolved = Path(backup_path).resolve()
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        resolved = path.with_name(f"{path.name}.backup-{stamp}")
    if resolved == path:
        raise ControlPlaneStateRecoveryError("backup path must differ from state path")
    if resolved.exists():
        raise ControlPlaneStateRecoveryError(f"backup path already exists: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _replace_without_streams(path: Path, excluded_streams: set[str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            for line in lines:
                if not line.strip():
                    handle.write(line)
                    continue
                decoded = json.loads(line)
                record = cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}
                stream_id = record.get("stream_id")
                if stream_id not in excluded_streams:
                    handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copystat(path, temporary_path)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _format_unrepairable_conflicts(conflicts: tuple[StateConflict, ...]) -> str:
    details = "; ".join(f"{conflict.idempotency_key}: {conflict.reason}" for conflict in conflicts)
    return f"Control Plane state has unrepairable idempotency conflicts: {details}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or repair Control Plane JSONL state")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--backup-path", type=Path)
    args = parser.parse_args(argv)
    try:
        report = (
            repair_control_plane_state(args.path, backup_path=args.backup_path)
            if args.repair
            else inspect_control_plane_state(args.path)
        )
    except ControlPlaneStateRecoveryError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(report.to_payload(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
