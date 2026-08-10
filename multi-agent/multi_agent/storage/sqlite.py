from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.domain.errors import (
    ApprovalNotFoundError,
    ApprovalStateError,
    RunNotFoundError,
)
from multi_agent.domain.models import (
    ApprovalStatus,
    EventKind,
    EventRecord,
    ProviderEvent,
    RunStatus,
    TaskStatus,
    WorkflowDefinition,
    utc_now,
)


class SQLiteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    workflow_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    provider_session_id TEXT,
                    final_output TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, task_id)
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY,
                    task_run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    provider_session_id TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    task_run_id TEXT,
                    attempt_id TEXT,
                    provider TEXT,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    raw_event_type TEXT
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    task_run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    provider_request_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    decided_by TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_task_runs_run ON task_runs(run_id);
                CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id, id);
                CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, status);
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def task_run_id(run_id: str, task_id: str) -> str:
        return f"{run_id}:{task_id}"

    def create_run(self, workflow: WorkflowDefinition) -> str:
        run_id = uuid4().hex
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (
                    run_id,
                    workflow.id,
                    workflow.model_dump_json(),
                    RunStatus.queued.value,
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO task_runs (
                    id, run_id, task_id, spec_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        self.task_run_id(run_id, task.id),
                        run_id,
                        task.id,
                        task.model_dump_json(),
                        TaskStatus.pending.value,
                        now,
                        now,
                    )
                    for task in workflow.tasks
                ],
            )
            connection.commit()
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        return self._run_dict(row)

    def get_workflow(self, run_id: str) -> WorkflowDefinition:
        run = self.get_run(run_id)
        return WorkflowDefinition.model_validate(run["workflow"])

    def list_task_runs(self, run_id: str) -> list[dict[str, Any]]:
        self.get_run(run_id)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    def get_task_run(self, run_id: str, task_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
        if row is None:
            raise RunNotFoundError(f"task {task_id!r} not found in run {run_id}")
        return self._task_dict(row)

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status.value, error, now, run_id),
            )
            if cursor.rowcount == 0:
                raise RunNotFoundError(f"run not found: {run_id}")
            connection.commit()

    def set_task_status(
        self,
        run_id: str,
        task_id: str,
        status: TaskStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE task_runs
                SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE run_id = ? AND task_id = ?
                """,
                (status.value, error_code, error_message, now, run_id, task_id),
            )
            if cursor.rowcount == 0:
                raise RunNotFoundError(f"task {task_id!r} not found in run {run_id}")
            connection.commit()

    def set_task_session(self, run_id: str, task_id: str, session_id: str) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE task_runs SET provider_session_id = ?, updated_at = ?
                WHERE run_id = ? AND task_id = ?
                """,
                (session_id, now, run_id, task_id),
            )
            connection.commit()

    def set_task_output(self, run_id: str, task_id: str, output: str | None) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE task_runs SET final_output = ?, updated_at = ?
                WHERE run_id = ? AND task_id = ?
                """,
                (output, now, run_id, task_id),
            )
            connection.commit()

    def start_attempt(self, run_id: str, task_id: str) -> tuple[str, int]:
        attempt_id = uuid4().hex
        now = utc_now().isoformat()
        task_run_id = self.task_run_id(run_id, task_id)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT attempt_count FROM task_runs WHERE id = ?", (task_run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"task {task_id!r} not found in run {run_id}")
            attempt_number = int(row["attempt_count"]) + 1
            connection.execute(
                "UPDATE task_runs SET attempt_count = ?, updated_at = ? WHERE id = ?",
                (attempt_number, now, task_run_id),
            )
            connection.execute(
                """
                INSERT INTO attempts (
                    id, task_run_id, attempt_number, status, started_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (attempt_id, task_run_id, attempt_number, "running", now),
            )
            connection.commit()
        return attempt_id, attempt_number

    def set_attempt_session(self, attempt_id: str, session_id: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE attempts SET provider_session_id = ? WHERE id = ?",
                (session_id, attempt_id),
            )
            connection.commit()

    def finish_attempt(
        self,
        attempt_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE attempts
                SET status = ?, error_code = ?, error_message = ?, ended_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    error_code,
                    error_message,
                    utc_now().isoformat(),
                    attempt_id,
                ),
            )
            connection.commit()

    def append_event(
        self,
        *,
        run_id: str,
        event: ProviderEvent,
        task_run_id: str | None = None,
        attempt_id: str | None = None,
        provider: str | None = None,
    ) -> EventRecord:
        occurred_at = utc_now()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (
                    run_id, task_run_id, attempt_id, provider, kind, occurred_at,
                    summary, payload_json, raw_event_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_run_id,
                    attempt_id,
                    provider,
                    event.kind.value,
                    occurred_at.isoformat(),
                    event.summary,
                    json.dumps(event.payload, ensure_ascii=False, default=str),
                    event.raw_event_type,
                ),
            )
            event_id = int(cursor.lastrowid)
            connection.commit()
        return EventRecord(
            event_id=event_id,
            run_id=run_id,
            task_run_id=task_run_id,
            attempt_id=attempt_id,
            provider=provider,
            kind=event.kind,
            occurred_at=occurred_at,
            summary=event.summary,
            payload=event.payload,
            raw_event_type=event.raw_event_type,
        )

    def list_events(self, run_id: str, after_id: int = 0) -> list[EventRecord]:
        self.get_run(run_id)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id",
                (run_id, after_id),
            ).fetchall()
        return [
            EventRecord(
                event_id=int(row["id"]),
                run_id=str(row["run_id"]),
                task_run_id=row["task_run_id"],
                attempt_id=row["attempt_id"],
                provider=row["provider"],
                kind=EventKind(row["kind"]),
                occurred_at=row["occurred_at"],
                summary=str(row["summary"]),
                payload=json.loads(row["payload_json"]),
                raw_event_type=row["raw_event_type"],
            )
            for row in rows
        ]

    def create_approval(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        provider: str,
        provider_request_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        approval_id = uuid4().hex
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    id, run_id, task_run_id, attempt_id, provider,
                    provider_request_id, status, request_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    run_id,
                    self.task_run_id(run_id, task_id),
                    attempt_id,
                    provider,
                    provider_request_id,
                    ApprovalStatus.pending.value,
                    json.dumps(request, ensure_ascii=False, default=str),
                    now,
                ),
            )
            connection.commit()
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(f"approval not found: {approval_id}")
        return self._approval_dict(row)

    def list_approvals(
        self,
        run_id: str,
        status: ApprovalStatus | None = None,
    ) -> list[dict[str, Any]]:
        self.get_run(run_id)
        query = "SELECT * FROM approvals WHERE run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if status is not None:
            query += " AND status = ?"
            params = (run_id, status.value)
        query += " ORDER BY created_at"
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._approval_dict(row) for row in rows]

    def resolve_approval(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        decided_by: str,
        reason: str | None,
    ) -> dict[str, Any]:
        if status == ApprovalStatus.pending:
            raise ValueError("a resolution cannot be pending")
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise ApprovalNotFoundError(f"approval not found: {approval_id}")
            if row["status"] != ApprovalStatus.pending.value:
                raise ApprovalStateError(
                    f"approval {approval_id} is already {row['status']}"
                )
            connection.execute(
                """
                UPDATE approvals
                SET status = ?, decided_by = ?, reason = ?, decided_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    decided_by,
                    reason,
                    utc_now().isoformat(),
                    approval_id,
                ),
            )
            connection.commit()
        return self.get_approval(approval_id)

    def recover_stale(self) -> dict[str, int]:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            task_cursor = connection.execute(
                """
                UPDATE task_runs SET status = ?, updated_at = ?
                WHERE run_id IN (
                    SELECT id FROM runs WHERE status IN (?, ?)
                )
                AND status NOT IN (?, ?, ?, ?, ?)
                """,
                (
                    TaskStatus.interrupted.value,
                    now,
                    RunStatus.queued.value,
                    RunStatus.running.value,
                    TaskStatus.succeeded.value,
                    TaskStatus.failed.value,
                    TaskStatus.cancelled.value,
                    TaskStatus.interrupted.value,
                    TaskStatus.blocked.value,
                ),
            )
            attempt_cursor = connection.execute(
                """
                UPDATE attempts SET status = ?, ended_at = ? WHERE status = 'running'
                """,
                (TaskStatus.interrupted.value, now),
            )
            connection.execute(
                """
                UPDATE approvals
                SET status = ?, decided_by = ?, reason = ?, decided_at = ?
                WHERE status = ? AND run_id IN (
                    SELECT id FROM runs WHERE status IN (?, ?)
                )
                """,
                (
                    ApprovalStatus.rejected.value,
                    "system:recovery",
                    "execution was interrupted during service recovery",
                    now,
                    ApprovalStatus.pending.value,
                    RunStatus.queued.value,
                    RunStatus.running.value,
                ),
            )
            run_cursor = connection.execute(
                """
                UPDATE runs SET status = ?, updated_at = ? WHERE status IN (?, ?)
                """,
                (
                    RunStatus.interrupted.value,
                    now,
                    RunStatus.queued.value,
                    RunStatus.running.value,
                ),
            )
            connection.commit()
        return {
            "runs": run_cursor.rowcount,
            "tasks": task_cursor.rowcount,
            "attempts": attempt_cursor.rowcount,
        }

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "workflow_id": str(row["workflow_id"]),
            "workflow": json.loads(row["workflow_json"]),
            "status": str(row["status"]),
            "error": row["error"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "run_id": str(row["run_id"]),
            "task_id": str(row["task_id"]),
            "spec": json.loads(row["spec_json"]),
            "status": str(row["status"]),
            "attempt_count": int(row["attempt_count"]),
            "provider_session_id": row["provider_session_id"],
            "final_output": row["final_output"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _approval_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "run_id": str(row["run_id"]),
            "task_run_id": str(row["task_run_id"]),
            "attempt_id": str(row["attempt_id"]),
            "provider": str(row["provider"]),
            "provider_request_id": str(row["provider_request_id"]),
            "status": str(row["status"]),
            "request": json.loads(row["request_json"]),
            "decided_by": row["decided_by"],
            "reason": row["reason"],
            "created_at": str(row["created_at"]),
            "decided_at": row["decided_at"],
        }
