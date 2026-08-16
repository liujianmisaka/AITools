from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any
from uuid import uuid4

from multi_agent.domain.errors import (
    ScheduledTaskConflictError,
    ScheduledTaskNotFoundError,
)
from multi_agent.domain.models import (
    ScheduledTaskDefinition,
    ScheduledTaskRunStatus,
    TriggerEventInput,
    utc_now,
)


class SQLiteScheduleStoreMixin:
    """SQLite persistence for schedule definitions and execution history."""

    def create_scheduled_task(
        self,
        task: ScheduledTaskDefinition,
    ) -> dict[str, Any]:
        if task.version != 1:
            raise ScheduledTaskConflictError(
                "a new scheduled task must start at version 1"
            )
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO scheduled_tasks (
                        id, version, name, schedule_type, schedule_json,
                        action_type, action_json, enabled, next_run_at,
                        last_run_at, last_status, last_error, scheduler_error,
                        created_at, updated_at, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                              NULL, ?, ?, NULL)
                    """,
                    (
                        task.id,
                        task.version,
                        task.name,
                        task.schedule_type,
                        self._json(task.schedule),
                        task.action_type,
                        self._json(task.action),
                        int(task.enabled),
                        now,
                        now,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise ScheduledTaskConflictError(
                    f"scheduled task {task.id!r} already exists"
                ) from exc
        return self.get_scheduled_task(task.id)

    def get_scheduled_task(
        self,
        task_id: str,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        query = "SELECT * FROM scheduled_tasks WHERE id = ?"
        if not include_archived:
            query += " AND archived_at IS NULL"
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(query, (task_id,)).fetchone()
        if row is None:
            raise ScheduledTaskNotFoundError(
                f"scheduled task not found: {task_id}"
            )
        return self._scheduled_task_dict(row)

    def list_scheduled_tasks(
        self,
        *,
        include_archived: bool = False,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if not include_archived:
            conditions.append("archived_at IS NULL")
        if enabled is not None:
            conditions.append("enabled = ?")
            params.append(int(enabled))
        query = "SELECT * FROM scheduled_tasks"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC, id DESC"
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._scheduled_task_dict(row) for row in rows]

    def update_scheduled_task(
        self,
        task_id: str,
        task: ScheduledTaskDefinition,
    ) -> dict[str, Any]:
        if task.id != task_id:
            raise ScheduledTaskConflictError(
                "scheduled task body id must match the path id"
            )
        next_version = task.version + 1
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_tasks
                SET version = ?, name = ?, schedule_type = ?, schedule_json = ?,
                    action_type = ?, action_json = ?, enabled = ?,
                    next_run_at = NULL, scheduler_error = NULL, updated_at = ?
                WHERE id = ? AND version = ? AND archived_at IS NULL
                """,
                (
                    next_version,
                    task.name,
                    task.schedule_type,
                    self._json(task.schedule),
                    task.action_type,
                    self._json(task.action),
                    int(task.enabled),
                    utc_now().isoformat(),
                    task_id,
                    task.version,
                ),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT version, archived_at FROM scheduled_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["archived_at"] is not None:
                    raise ScheduledTaskNotFoundError(
                        f"scheduled task not found: {task_id}"
                    )
                raise ScheduledTaskConflictError(
                    f"scheduled task {task_id!r} is at version {row['version']}, "
                    f"not {task.version}"
                )
            connection.commit()
        return self.get_scheduled_task(task_id)

    def set_scheduled_task_enabled(
        self,
        task_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT enabled, archived_at FROM scheduled_tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None or row["archived_at"] is not None:
                raise ScheduledTaskNotFoundError(
                    f"scheduled task not found: {task_id}"
                )
            if bool(row["enabled"]) == enabled:
                return self.get_scheduled_task(task_id)
            cursor = connection.execute(
                """
                UPDATE scheduled_tasks
                SET version = version + 1, enabled = ?, next_run_at = NULL,
                    scheduler_error = NULL, updated_at = ?
                WHERE id = ? AND archived_at IS NULL
                """,
                (int(enabled), utc_now().isoformat(), task_id),
            )
            if cursor.rowcount == 0:
                raise ScheduledTaskNotFoundError(
                    f"scheduled task not found: {task_id}"
                )
            connection.commit()
        return self.get_scheduled_task(task_id)

    def archive_scheduled_task(self, task_id: str) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_tasks
                SET version = version + 1, enabled = 0, next_run_at = NULL,
                    scheduler_error = NULL,
                    archived_at = COALESCE(archived_at, ?), updated_at = ?
                WHERE id = ? AND archived_at IS NULL
                """,
                (now, now, task_id),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT id FROM scheduled_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise ScheduledTaskNotFoundError(
                        f"scheduled task not found: {task_id}"
                    )
            connection.commit()
        return self.get_scheduled_task(task_id, include_archived=True)

    def set_scheduled_task_next_run(
        self,
        task_id: str,
        next_run_at: str | None,
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE scheduled_tasks
                SET next_run_at = ?, scheduler_error = NULL, updated_at = ?
                WHERE id = ? AND archived_at IS NULL
                """,
                (next_run_at, utc_now().isoformat(), task_id),
            )
            connection.commit()

    def set_scheduled_task_runtime_error(
        self,
        task_id: str,
        error: str,
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE scheduled_tasks
                SET next_run_at = NULL, scheduler_error = ?, updated_at = ?
                WHERE id = ? AND archived_at IS NULL
                """,
                (
                    error,
                    utc_now().isoformat(),
                    task_id,
                ),
            )
            connection.commit()

    def start_scheduled_task_run(
        self,
        task_id: str,
        *,
        scheduled_for: str | None,
    ) -> dict[str, Any]:
        self.get_scheduled_task(task_id)
        run_id = uuid4().hex
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO scheduled_task_runs (
                    id, scheduled_task_id, scheduled_for, status,
                    result_json, error, started_at, finished_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)
                """,
                (
                    run_id,
                    task_id,
                    scheduled_for,
                    ScheduledTaskRunStatus.running.value,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE scheduled_tasks
                SET last_run_at = ?, last_status = ?, last_error = NULL,
                    updated_at = ? WHERE id = ?
                """,
                (now, ScheduledTaskRunStatus.running.value, now, task_id),
            )
            connection.commit()
        return self.get_scheduled_task_run(run_id)

    def finish_scheduled_task_run(
        self,
        run_id: str,
        status: ScheduledTaskRunStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        internal_event: TriggerEventInput | None = None,
    ) -> dict[str, Any]:
        if status == ScheduledTaskRunStatus.running:
            raise ValueError("a finished scheduled task run cannot be running")
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT scheduled_task_id FROM scheduled_task_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ScheduledTaskNotFoundError(
                    f"scheduled task run not found: {run_id}"
                )
            cursor = connection.execute(
                """
                UPDATE scheduled_task_runs
                SET status = ?, result_json = ?, error = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    status.value,
                    None if result is None else self._json(result),
                    error,
                    now,
                    run_id,
                    ScheduledTaskRunStatus.running.value,
                ),
            )
            if cursor.rowcount == 0:
                current = connection.execute(
                    "SELECT status FROM scheduled_task_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                raise ScheduledTaskConflictError(
                    f"scheduled task run {run_id!r} is already "
                    f"{current['status']}"
                )
            connection.execute(
                """
                UPDATE scheduled_tasks
                SET last_status = ?, last_error = ?, updated_at = ? WHERE id = ?
                """,
                (status.value, error, now, row["scheduled_task_id"]),
            )
            if internal_event is not None:
                self._insert_internal_event_row(connection, internal_event)
            connection.commit()
        return self.get_scheduled_task_run(run_id)

    def get_scheduled_task_run(self, run_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_task_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ScheduledTaskNotFoundError(
                f"scheduled task run not found: {run_id}"
            )
        return self._scheduled_task_run_dict(row)

    def count_scheduled_task_runs(self, task_id: str) -> int:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM scheduled_task_runs "
                "WHERE scheduled_task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def list_scheduled_task_runs(
        self,
        task_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.get_scheduled_task(task_id, include_archived=True)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM scheduled_task_runs
                WHERE scheduled_task_id = ?
                ORDER BY started_at DESC, id DESC LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [self._scheduled_task_run_dict(row) for row in rows]

    def recover_scheduled_task_runs(self) -> int:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_task_runs
                SET status = ?, error = ?, finished_at = ? WHERE status = ?
                """,
                (
                    ScheduledTaskRunStatus.interrupted.value,
                    "scheduler stopped before the run completed",
                    now,
                    ScheduledTaskRunStatus.running.value,
                ),
            )
            connection.execute(
                """
                UPDATE scheduled_tasks
                SET last_status = ?, last_error = ?, updated_at = ?
                WHERE id IN (
                    SELECT scheduled_task_id FROM scheduled_task_runs
                    WHERE status = ? AND finished_at = ?
                )
                """,
                (
                    ScheduledTaskRunStatus.interrupted.value,
                    "scheduler stopped before the run completed",
                    now,
                    ScheduledTaskRunStatus.interrupted.value,
                    now,
                ),
            )
            connection.commit()
        return cursor.rowcount

    @staticmethod
    def _scheduled_task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "version": int(row["version"]),
            "name": str(row["name"]),
            "schedule_type": str(row["schedule_type"]),
            "schedule": json.loads(row["schedule_json"]),
            "action_type": str(row["action_type"]),
            "action": json.loads(row["action_json"]),
            "enabled": bool(row["enabled"]),
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "last_status": row["last_status"],
            "last_error": row["last_error"],
            "scheduler_error": row["scheduler_error"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "archived_at": row["archived_at"],
        }

    @staticmethod
    def _scheduled_task_run_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "scheduled_task_id": str(row["scheduled_task_id"]),
            "scheduled_for": row["scheduled_for"],
            "status": str(row["status"]),
            "result": (
                None
                if row["result_json"] is None
                else json.loads(row["result_json"])
            ),
            "error": row["error"],
            "started_at": str(row["started_at"]),
            "finished_at": row["finished_at"],
        }
