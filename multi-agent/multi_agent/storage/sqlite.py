from __future__ import annotations

import json
import sqlite3
import threading
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.domain.errors import (
    ApprovalNotFoundError,
    ApprovalStateError,
    WorkflowInstanceCursorError,
    WorkflowInstanceNotFoundError,
    WorkflowTemplateCursorError,
    WorkflowTemplateNotFoundError,
    WorkflowTemplateVersionConflictError,
)
from multi_agent.domain.models import (
    ApprovalStatus,
    EventKind,
    EventRecord,
    ProviderEvent,
    TaskInstanceStatus,
    WorkflowDefinition,
    WorkflowInstanceStatus,
    utc_now,
)
from multi_agent.storage.schema import SCHEMA_SQL, SCHEMA_VERSION


_LEGACY_SCHEMA_TABLES = frozenset(
    {
        "schema_migrations",
        "workflows",
        "runs",
        "task_runs",
        "attempts",
        "events",
        "approvals",
    }
)
_REQUIRED_SCHEMA_TABLES = frozenset(
    {
        "schema_metadata",
        "workflow_templates",
        "workflow_instances",
        "task_instances",
        "execution_attempts",
        "workflow_events",
        "workflow_approvals",
    }
)


class SQLiteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            existing_tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                ).fetchall()
            }
            legacy_tables = sorted(existing_tables & _LEGACY_SCHEMA_TABLES)
            if legacy_tables:
                raise RuntimeError(
                    "legacy database schema is unsupported; recreate the database "
                    f"before starting: {', '.join(legacy_tables)}"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    version INTEGER NOT NULL
                )
                """
            )
            connection.commit()
            metadata = connection.execute(
                "SELECT version FROM schema_metadata WHERE id = 1"
            ).fetchone()
            if metadata is not None and int(metadata["version"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    "database schema version is incompatible with the current "
                    "baseline; recreate the database before starting: "
                    f"{metadata['version']}"
                )
            if metadata is None:
                baseline = f"""
                BEGIN IMMEDIATE;
                {SCHEMA_SQL}
                INSERT INTO schema_metadata(id, version)
                VALUES (1, {SCHEMA_VERSION});
                COMMIT;
                """
                try:
                    connection.executescript(baseline)
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
            final_tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                ).fetchall()
            }
            missing_tables = sorted(_REQUIRED_SCHEMA_TABLES - final_tables)
            if missing_tables:
                raise RuntimeError(
                    "database schema does not match the current baseline; recreate "
                    "the database before starting: missing "
                    + ", ".join(missing_tables)
                )
            connection.execute("PRAGMA optimize")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def task_instance_id(workflow_instance_id: str, task_id: str) -> str:
        return f"{workflow_instance_id}:{task_id}"

    @staticmethod
    def _cursor(sort_value: str, record_id: str) -> str:
        payload = json.dumps(
            {"sort": sort_value, "id": record_id},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, *, instance: bool = False) -> tuple[str, str]:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(
                urlsafe_b64decode((cursor + padding).encode("ascii")).decode("utf-8")
            )
            sort_value = payload["sort"]
            record_id = payload["id"]
            if not isinstance(sort_value, str) or not isinstance(record_id, str):
                raise TypeError("cursor fields must be strings")
            if not sort_value or not record_id:
                raise ValueError("cursor fields cannot be empty")
            return sort_value, record_id
        except (
            BinasciiError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            error_type = (
                WorkflowInstanceCursorError if instance else WorkflowTemplateCursorError
            )
            raise error_type("cursor is invalid") from exc

    def create_template(
        self,
        workflow: WorkflowDefinition,
    ) -> dict[str, Any]:
        if workflow.version != 1:
            raise WorkflowTemplateVersionConflictError(
                "a new workflow template must start at version 1"
            )
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO workflow_templates (
                        id, version, name, definition_json, task_count,
                        created_at, updated_at, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        workflow.id,
                        workflow.version,
                        workflow.name,
                        workflow.model_dump_json(),
                        len(workflow.tasks),
                        now,
                        now,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise WorkflowTemplateVersionConflictError(
                    f"workflow template {workflow.id!r} already exists"
                ) from exc
        return self.get_template(workflow.id)

    def get_template(
        self,
        template_id: str,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        query = "SELECT * FROM workflow_templates WHERE id = ?"
        if not include_archived:
            query += " AND archived_at IS NULL"
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(query, (template_id,)).fetchone()
        if row is None:
            raise WorkflowTemplateNotFoundError(
                f"workflow template not found: {template_id}"
            )
        return self._template_dict(row)

    def list_templates(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if not include_archived:
            conditions.append("archived_at IS NULL")
        if cursor:
            updated_at, template_id = self._decode_cursor(cursor)
            conditions.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend((updated_at, updated_at, template_id))
        query = "SELECT * FROM workflow_templates"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit + 1)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()

        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        items = [self._template_summary(row) for row in visible_rows]
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = self._cursor(
                str(last["updated_at"]),
                str(last["id"]),
            )
        return {"items": items, "next_cursor": next_cursor}

    def update_template(
        self,
        template_id: str,
        workflow: WorkflowDefinition,
    ) -> dict[str, Any]:
        if workflow.id != template_id:
            raise WorkflowTemplateVersionConflictError(
                "workflow template body id must match the path id"
            )
        next_version = workflow.version + 1
        updated = workflow.model_copy(update={"version": next_version})
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_templates
                SET version = ?, name = ?, definition_json = ?, task_count = ?,
                    updated_at = ?
                WHERE id = ? AND version = ? AND archived_at IS NULL
                """,
                (
                    next_version,
                    updated.name,
                    updated.model_dump_json(),
                    len(updated.tasks),
                    now,
                    template_id,
                    workflow.version,
                ),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    "SELECT version, archived_at FROM workflow_templates WHERE id = ?",
                    (template_id,),
                ).fetchone()
                if existing is None or existing["archived_at"] is not None:
                    raise WorkflowTemplateNotFoundError(
                        f"workflow template not found: {template_id}"
                    )
                raise WorkflowTemplateVersionConflictError(
                    f"workflow template {template_id!r} is at version "
                    f"{existing['version']}, "
                    f"not {workflow.version}"
                )
            connection.commit()
        return self.get_template(template_id)

    def archive_template(self, template_id: str) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT archived_at FROM workflow_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if row is None:
                raise WorkflowTemplateNotFoundError(
                    f"workflow template not found: {template_id}"
                )
            if row["archived_at"] is None:
                connection.execute(
                    """
                    UPDATE workflow_templates
                    SET archived_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, template_id),
                )
                connection.commit()
        return self.get_template(template_id, include_archived=True)

    def create_instance(
        self,
        workflow: WorkflowDefinition,
        *,
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> str:
        if (template_id is None) != (template_version is None):
            raise ValueError("template_id and template_version must be supplied together")
        if template_id is not None and (
            workflow.id != template_id or workflow.version != template_version
        ):
            raise WorkflowTemplateVersionConflictError(
                "workflow definition does not match the requested template version"
            )
        instance_id = uuid4().hex
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO workflow_instances (
                    id, template_id, template_version, source, name,
                    definition_json, task_count, status, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    instance_id,
                    template_id,
                    template_version,
                    "template" if template_id is not None else "ad_hoc",
                    workflow.name,
                    workflow.model_dump_json(),
                    len(workflow.tasks),
                    WorkflowInstanceStatus.queued.value,
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO task_instances (
                    id, workflow_instance_id, task_id, spec_json, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        self.task_instance_id(instance_id, task.id),
                        instance_id,
                        task.id,
                        task.model_dump_json(),
                        TaskInstanceStatus.pending.value,
                        now,
                        now,
                    )
                    for task in workflow.tasks
                ],
            )
            connection.commit()
        return instance_id

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT workflow_instances.*,
                    (
                        SELECT COUNT(*) FROM task_instances
                        WHERE workflow_instance_id = workflow_instances.id
                          AND status IN ('succeeded', 'failed', 'cancelled',
                                         'interrupted', 'blocked')
                    ) AS completed_task_count
                FROM workflow_instances WHERE id = ?
                """,
                (instance_id,),
            ).fetchone()
        if row is None:
            raise WorkflowInstanceNotFoundError(
                f"workflow instance not found: {instance_id}"
            )
        return self._instance_dict(row)

    def list_instances(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: WorkflowInstanceStatus | None = None,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("workflow_instances.status = ?")
            params.append(status.value)
        if cursor:
            created_at, instance_id = self._decode_cursor(cursor, instance=True)
            conditions.append(
                "(workflow_instances.created_at < ? OR "
                "(workflow_instances.created_at = ? AND workflow_instances.id < ?))"
            )
            params.extend((created_at, created_at, instance_id))
        query = """
            SELECT workflow_instances.*,
                (
                    SELECT COUNT(*) FROM task_instances
                    WHERE workflow_instance_id = workflow_instances.id
                      AND status IN ('succeeded', 'failed', 'cancelled',
                                     'interrupted', 'blocked')
                ) AS completed_task_count
            FROM workflow_instances
        """
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit + 1)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        items = [self._instance_summary(row) for row in visible_rows]
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = self._cursor(str(last["created_at"]), str(last["id"]))
        return {"items": items, "next_cursor": next_cursor}

    def get_instance_definition(self, instance_id: str) -> WorkflowDefinition:
        instance = self.get_instance(instance_id)
        return WorkflowDefinition.model_validate(instance["definition"])

    def list_task_instances(self, instance_id: str) -> list[dict[str, Any]]:
        self.get_instance(instance_id)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_instances
                WHERE workflow_instance_id = ? ORDER BY rowid
                """,
                (instance_id,),
            ).fetchall()
        return [self._task_instance_dict(row) for row in rows]

    def get_task_instance(self, instance_id: str, task_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM task_instances
                WHERE workflow_instance_id = ? AND task_id = ?
                """,
                (instance_id, task_id),
            ).fetchone()
        if row is None:
            raise WorkflowInstanceNotFoundError(
                f"task {task_id!r} not found in workflow instance {instance_id}"
            )
        return self._task_instance_dict(row)

    def set_instance_status(
        self,
        instance_id: str,
        status: WorkflowInstanceStatus,
        *,
        error: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_instances
                SET status = ?, error = ?, updated_at = ? WHERE id = ?
                """,
                (status.value, error, now, instance_id),
            )
            if cursor.rowcount == 0:
                raise WorkflowInstanceNotFoundError(
                    f"workflow instance not found: {instance_id}"
                )
            connection.commit()

    def set_task_status(
        self,
        instance_id: str,
        task_id: str,
        status: TaskInstanceStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE task_instances
                SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE workflow_instance_id = ? AND task_id = ?
                """,
                (status.value, error_code, error_message, now, instance_id, task_id),
            )
            if cursor.rowcount == 0:
                raise WorkflowInstanceNotFoundError(
                    f"task {task_id!r} not found in workflow instance {instance_id}"
                )
            connection.commit()

    def set_task_session(
        self, instance_id: str, task_id: str, session_id: str
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE task_instances SET provider_session_id = ?, updated_at = ?
                WHERE workflow_instance_id = ? AND task_id = ?
                """,
                (session_id, now, instance_id, task_id),
            )
            connection.commit()

    def set_task_output(
        self, instance_id: str, task_id: str, output: str | None
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE task_instances SET final_output = ?, updated_at = ?
                WHERE workflow_instance_id = ? AND task_id = ?
                """,
                (output, now, instance_id, task_id),
            )
            connection.commit()

    def start_attempt(self, instance_id: str, task_id: str) -> tuple[str, int]:
        attempt_id = uuid4().hex
        now = utc_now().isoformat()
        task_instance_id = self.task_instance_id(instance_id, task_id)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT attempt_count FROM task_instances WHERE id = ?",
                (task_instance_id,),
            ).fetchone()
            if row is None:
                raise WorkflowInstanceNotFoundError(
                    f"task {task_id!r} not found in workflow instance {instance_id}"
                )
            attempt_number = int(row["attempt_count"]) + 1
            connection.execute(
                """
                UPDATE task_instances
                SET attempt_count = ?, updated_at = ? WHERE id = ?
                """,
                (attempt_number, now, task_instance_id),
            )
            connection.execute(
                """
                INSERT INTO execution_attempts (
                    id, task_instance_id, attempt_number, status, started_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (attempt_id, task_instance_id, attempt_number, "running", now),
            )
            connection.commit()
        return attempt_id, attempt_number

    def set_attempt_session(self, attempt_id: str, session_id: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE execution_attempts SET provider_session_id = ? WHERE id = ?",
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
                UPDATE execution_attempts
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
        instance_id: str,
        event: ProviderEvent,
        task_instance_id: str | None = None,
        attempt_id: str | None = None,
        provider: str | None = None,
    ) -> EventRecord:
        occurred_at = utc_now()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO workflow_events (
                    workflow_instance_id, task_instance_id, execution_attempt_id,
                    provider, kind, occurred_at,
                    summary, payload_json, raw_event_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    task_instance_id,
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
            workflow_instance_id=instance_id,
            task_instance_id=task_instance_id,
            execution_attempt_id=attempt_id,
            provider=provider,
            kind=event.kind,
            occurred_at=occurred_at,
            summary=event.summary,
            payload=event.payload,
            raw_event_type=event.raw_event_type,
        )

    def list_events(
        self, instance_id: str, after_id: int = 0
    ) -> list[EventRecord]:
        self.get_instance(instance_id)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_events
                WHERE workflow_instance_id = ? AND id > ? ORDER BY id
                """,
                (instance_id, after_id),
            ).fetchall()
        return [
            EventRecord(
                event_id=int(row["id"]),
                workflow_instance_id=str(row["workflow_instance_id"]),
                task_instance_id=row["task_instance_id"],
                execution_attempt_id=row["execution_attempt_id"],
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
        instance_id: str,
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
                INSERT INTO workflow_approvals (
                    id, workflow_instance_id, task_instance_id,
                    execution_attempt_id, provider,
                    provider_request_id, status, request_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    instance_id,
                    self.task_instance_id(instance_id, task_id),
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
                "SELECT * FROM workflow_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(f"approval not found: {approval_id}")
        return self._approval_dict(row)

    def list_approvals(
        self,
        instance_id: str,
        status: ApprovalStatus | None = None,
    ) -> list[dict[str, Any]]:
        self.get_instance(instance_id)
        query = "SELECT * FROM workflow_approvals WHERE workflow_instance_id = ?"
        params: tuple[Any, ...] = (instance_id,)
        if status is not None:
            query += " AND status = ?"
            params = (instance_id, status.value)
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
                "SELECT status FROM workflow_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise ApprovalNotFoundError(f"approval not found: {approval_id}")
            if row["status"] != ApprovalStatus.pending.value:
                raise ApprovalStateError(
                    f"approval {approval_id} is already {row['status']}"
                )
            connection.execute(
                """
                UPDATE workflow_approvals
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
                UPDATE task_instances SET status = ?, updated_at = ?
                WHERE workflow_instance_id IN (
                    SELECT id FROM workflow_instances WHERE status IN (?, ?)
                )
                AND status NOT IN (?, ?, ?, ?, ?)
                """,
                (
                    TaskInstanceStatus.interrupted.value,
                    now,
                    WorkflowInstanceStatus.queued.value,
                    WorkflowInstanceStatus.running.value,
                    TaskInstanceStatus.succeeded.value,
                    TaskInstanceStatus.failed.value,
                    TaskInstanceStatus.cancelled.value,
                    TaskInstanceStatus.interrupted.value,
                    TaskInstanceStatus.blocked.value,
                ),
            )
            attempt_cursor = connection.execute(
                """
                UPDATE execution_attempts
                SET status = ?, ended_at = ? WHERE status = 'running'
                """,
                (TaskInstanceStatus.interrupted.value, now),
            )
            connection.execute(
                """
                UPDATE workflow_approvals
                SET status = ?, decided_by = ?, reason = ?, decided_at = ?
                WHERE status = ? AND workflow_instance_id IN (
                    SELECT id FROM workflow_instances WHERE status IN (?, ?)
                )
                """,
                (
                    ApprovalStatus.rejected.value,
                    "system:recovery",
                    "execution was interrupted during service recovery",
                    now,
                    ApprovalStatus.pending.value,
                    WorkflowInstanceStatus.queued.value,
                    WorkflowInstanceStatus.running.value,
                ),
            )
            instance_cursor = connection.execute(
                """
                UPDATE workflow_instances
                SET status = ?, updated_at = ? WHERE status IN (?, ?)
                """,
                (
                    WorkflowInstanceStatus.interrupted.value,
                    now,
                    WorkflowInstanceStatus.queued.value,
                    WorkflowInstanceStatus.running.value,
                ),
            )
            connection.commit()
        return {
            "instances": instance_cursor.rowcount,
            "task_instances": task_cursor.rowcount,
            "attempts": attempt_cursor.rowcount,
        }

    @staticmethod
    def _instance_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "template_id": row["template_id"],
            "template_version": row["template_version"],
            "source": str(row["source"]),
            "name": str(row["name"]),
            "task_count": int(row["task_count"]),
            "completed_task_count": int(row["completed_task_count"]),
            "status": str(row["status"]),
            "error": row["error"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @classmethod
    def _instance_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        return {
            **cls._instance_summary(row),
            "definition": json.loads(row["definition_json"]),
        }

    @staticmethod
    def _template_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "version": int(row["version"]),
            "name": str(row["name"]),
            "task_count": int(row["task_count"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "archived_at": row["archived_at"],
        }

    @classmethod
    def _template_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        return {
            **cls._template_summary(row),
            "definition": json.loads(row["definition_json"]),
        }

    @staticmethod
    def _task_instance_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "workflow_instance_id": str(row["workflow_instance_id"]),
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
            "workflow_instance_id": str(row["workflow_instance_id"]),
            "task_instance_id": str(row["task_instance_id"]),
            "execution_attempt_id": str(row["execution_attempt_id"]),
            "provider": str(row["provider"]),
            "provider_request_id": str(row["provider_request_id"]),
            "status": str(row["status"]),
            "request": json.loads(row["request_json"]),
            "decided_by": row["decided_by"],
            "reason": row["reason"],
            "created_at": str(row["created_at"]),
            "decided_at": row["decided_at"],
        }
