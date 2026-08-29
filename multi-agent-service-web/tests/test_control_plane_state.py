from __future__ import annotations

import json
from pathlib import Path

import pytest
from aitools_service_manager.control_plane_state import (
    ControlPlaneStateRecoveryError,
    inspect_control_plane_state,
    repair_control_plane_state,
)


def _record(
    stream_id: str,
    sequence: int,
    event_id: str,
    event_type: str,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "stream_id": stream_id,
        "sequence": sequence,
        "event_id": event_id,
        "event_type": event_type,
        "payload": payload,
        "schema_version": 1,
        "occurred_at": "2026-08-29T00:00:00+00:00",
    }


def _created(stream_id: str, key: str) -> dict[str, object]:
    delegation_id = stream_id.removeprefix("delegation:")
    return _record(
        stream_id,
        1,
        "delegation-created",
        "delegation.created",
        {
            "request": {"delegation_id": delegation_id, "idempotency_key": key},
            "ref": {"delegation_id": delegation_id},
        },
    )


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_inspect_classifies_creation_only_orphan(tmp_path: Path) -> None:
    canonical = "delegation:canonical"
    orphan = "delegation:orphan"
    path = tmp_path / "control-plane.jsonl"
    _write(
        path,
        [
            _created(canonical, "same-key"),
            _record(canonical, 2, "final", "delegation.finalized", {"report": {}}),
            _created(orphan, "same-key"),
        ],
    )

    report = inspect_control_plane_state(path)

    assert report.repairable
    assert report.conflicts[0].canonical_stream == canonical
    assert report.conflicts[0].orphan_streams == (orphan,)


def test_repair_creates_backup_and_removes_only_orphan_stream(tmp_path: Path) -> None:
    canonical = "delegation:canonical"
    orphan = "delegation:orphan"
    path = tmp_path / "control-plane.jsonl"
    backup = tmp_path / "control-plane.backup.jsonl"
    canonical_final = _record(canonical, 2, "final", "delegation.finalized", {"report": {}})
    _write(path, [_created(canonical, "same-key"), canonical_final, _created(orphan, "same-key")])
    original = path.read_bytes()

    result = repair_control_plane_state(path, backup_path=backup)

    assert result.conflicts == ()
    assert result.backup_path == backup.resolve()
    assert result.removed_streams == (orphan,)
    assert backup.read_bytes() == original
    repaired = inspect_control_plane_state(path)
    assert repaired.event_count == 2
    content = path.read_text(encoding="utf-8")
    assert "delegation:canonical" in content
    assert "delegation:orphan" not in content


def test_repair_fails_closed_for_two_non_creation_streams(tmp_path: Path) -> None:
    first = "delegation:first"
    second = "delegation:second"
    path = tmp_path / "control-plane.jsonl"
    _write(
        path,
        [
            _created(first, "same-key"),
            _record(first, 2, "active", "delegation.activation_active", {}),
            _created(second, "same-key"),
            _record(second, 2, "active", "delegation.activation_active", {}),
        ],
    )
    original = path.read_bytes()

    with pytest.raises(ControlPlaneStateRecoveryError, match="unrepairable"):
        repair_control_plane_state(path)

    assert path.read_bytes() == original


def test_repair_does_not_overwrite_existing_backup(tmp_path: Path) -> None:
    canonical = "delegation:canonical"
    orphan = "delegation:orphan"
    path = tmp_path / "control-plane.jsonl"
    backup = tmp_path / "existing-backup.jsonl"
    _write(
        path,
        [
            _created(canonical, "same-key"),
            _record(canonical, 2, "final", "delegation.finalized", {}),
            _created(orphan, "same-key"),
        ],
    )
    original = path.read_bytes()
    backup.write_text("existing", encoding="utf-8")

    with pytest.raises(ControlPlaneStateRecoveryError, match="already exists"):
        repair_control_plane_state(path, backup_path=backup)

    assert path.read_bytes() == original
    assert backup.read_text(encoding="utf-8") == "existing"


def test_inspect_rejects_sequence_gap(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.jsonl"
    _write(
        path,
        [
            _created("delegation:one", "key"),
            _record("delegation:one", 3, "final", "delegation.finalized", {}),
        ],
    )

    with pytest.raises(ControlPlaneStateRecoveryError, match="expected sequence 2"):
        inspect_control_plane_state(path)


def test_inspect_without_conflict_does_not_require_repair(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.jsonl"
    _write(path, [_created("delegation:one", "unique-key")])

    report = repair_control_plane_state(path)

    assert report.conflicts == ()
    assert report.backup_path is None
    assert report.removed_streams == ()
