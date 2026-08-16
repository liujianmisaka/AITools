from __future__ import annotations

import json
import sqlite3
import threading
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.domain.errors import (
    ApprovalNotFoundError,
    ApprovalStateError,
    TriggerBindingConflictError,
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
    TriggerEventInput,
    WorkItemSeed,
    WorkflowInstanceStatus,
    utc_now,
)
from multi_agent.storage.schema import SCHEMA_SQL, SCHEMA_VERSION
from multi_agent.storage.outbox_sqlite import SQLiteOutboxStoreMixin
from multi_agent.storage.schedule_sqlite import SQLiteScheduleStoreMixin
from multi_agent.storage.trigger_sqlite import SQLiteTriggerStoreMixin


_LEGACY_SCHEMA_TABLES = frozenset(
    {
        "schema_migrations",
        "workflows",
        "runs",
        "task_runs",
        "task_instances",
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
        "work_items",
        "execution_attempts",
        "workflow_events",
        "workflow_approvals",
        "trigger_bindings",
        "trigger_events",
        "trigger_deliveries",
        "trigger_source_state",
        "internal_event_outbox",
        "scheduled_tasks",
        "scheduled_task_runs",
    }
)


class SQLiteStore(
    SQLiteScheduleStoreMixin,
    SQLiteTriggerStoreMixin,
    SQLiteOutboxStoreMixin,
):
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
            if "schema_metadata" not in existing_tables and existing_tables:
                legacy_tables = sorted(existing_tables & _LEGACY_SCHEMA_TABLES)
                if legacy_tables:
                    raise RuntimeError(
                        "legacy database schema is unsupported; recreate the "
                        "database before starting: " + ", ".join(legacy_tables)
                    )
                raise RuntimeError(
                    "database schema is incomplete and has no version metadata; "
                    "recreate the database before starting"
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
            legacy_tables = sorted(existing_tables & _LEGACY_SCHEMA_TABLES)
            if metadata is None and legacy_tables:
                raise RuntimeError(
                    "legacy database schema is unsupported; recreate the database "
                    f"before starting: {', '.join(legacy_tables)}"
                )
            unexpected_tables = existing_tables - {
                "schema_metadata",
                "sqlite_sequence",
            }
            if metadata is None and unexpected_tables:
                raise RuntimeError(
                    "database schema is incomplete and has no version record; "
                    "recreate the database before starting"
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
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def work_item_id(
        workflow_instance_id: str,
        logical_key: str,
        activation_number: int = 1,
    ) -> tuple[str, bool]:
        return f"{workflow_instance_id}:{logical_key}:{activation_number}"

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
                urlsafe_b64decode((cursor + padding).encode("ascii")).decode(
                    "utf-8"
                )
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
                WorkflowInstanceCursorError
                if instance
                else WorkflowTemplateCursorError
            )
            raise error_type("cursor is invalid") from exc

    def create_template(
        self,
        *,
        template_id: str,
        version: int,
        kind: str,
        definition_schema_version: int,
        name: str,
        definition: dict[str, Any],
        work_item_count: int,
    ) -> dict[str, Any]:
        if version != 1:
            raise WorkflowTemplateVersionConflictError(
                "a new workflow template must start at version 1"
            )
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO workflow_templates (
                        id, version, kind, definition_schema_version, name,
                        definition_json, work_item_count,
                        created_at, updated_at, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        template_id,
                        version,
                        kind,
                        definition_schema_version,
                        name,
                        self._json(definition),
                        work_item_count,
                        now,
                        now,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise WorkflowTemplateVersionConflictError(
                    f"workflow template {template_id!r} already exists"
                ) from exc
        return self.get_template(template_id)

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
            next_cursor = self._cursor(str(last["updated_at"]), str(last["id"]))
        return {"items": items, "next_cursor": next_cursor}

    def update_template(
        self,
        template_id: str,
        *,
        expected_version: int,
        kind: str,
        definition_schema_version: int,
        name: str,
        definition: dict[str, Any],
        work_item_count: int,
    ) -> dict[str, Any]:
        next_version = expected_version + 1
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_templates
                SET version = ?, kind = ?, definition_schema_version = ?,
                    name = ?, definition_json = ?, work_item_count = ?,
                    updated_at = ?
                WHERE id = ? AND version = ? AND archived_at IS NULL
                """,
                (
                    next_version,
                    kind,
                    definition_schema_version,
                    name,
                    self._json(definition),
                    work_item_count,
                    now,
                    template_id,
                    expected_version,
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
                    f"{existing['version']}, not {expected_version}"
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
                active_binding = connection.execute(
                    """
                    SELECT id FROM trigger_bindings
                    WHERE template_id = ? AND enabled = 1 AND archived_at IS NULL
                    LIMIT 1
                    """,
                    (template_id,),
                ).fetchone()
                if active_binding is not None:
                    raise TriggerBindingConflictError(
                        "disable or archive active trigger bindings before "
                        f"archiving template {template_id!r}"
                    )
                connection.execute(
                    """
                    UPDATE workflow_templates
                    SET archived_at = ?, updated_at = ? WHERE id = ?
                    """,
                    (now, now, template_id),
                )
                connection.commit()
        return self.get_template(template_id, include_archived=True)

    def create_instance(
        self,
        *,
        kind: str,
        definition_schema_version: int,
        name: str,
        definition: dict[str, Any],
        work_items: Sequence[WorkItemSeed],
        template_id: str | None = None,
        template_version: int | None = None,
        input_data: dict[str, Any] | None = None,
        cause_type: str = "manual",
        trigger_binding_id: str | None = None,
        trigger_event_id: str | None = None,
        enqueue_created_event: bool = False,
    ) -> str:
        if (template_id is None) != (template_version is None):
            raise ValueError("template_id and template_version must be supplied together")
        if cause_type == "trigger":
            if trigger_binding_id is None or trigger_event_id is None:
                raise ValueError("trigger cause requires binding and event IDs")
        elif trigger_binding_id is not None or trigger_event_id is not None:
            raise ValueError("manual cause cannot include trigger IDs")

        instance_id = uuid4().hex
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            if template_id is not None:
                template = connection.execute(
                    """
                    SELECT version, kind, archived_at FROM workflow_templates
                    WHERE id = ?
                    """,
                    (template_id,),
                ).fetchone()
                if template is None or template["archived_at"] is not None:
                    raise WorkflowTemplateNotFoundError(
                        f"workflow template not found: {template_id}"
                    )
                if int(template["version"]) != template_version:
                    raise WorkflowTemplateVersionConflictError(
                        f"workflow template {template_id!r} is at version "
                        f"{template['version']}, not {template_version}"
                    )
                if str(template["kind"]) != kind:
                    raise WorkflowTemplateVersionConflictError(
                        "workflow definition kind does not match the template"
                    )
            try:
                connection.execute(
                    """
                    INSERT INTO workflow_instances (
                        id, template_id, template_version, source, kind,
                        definition_schema_version, name, definition_json,
                        input_json, runtime_state_json, revision, work_item_count,
                        status, cause_type, trigger_binding_id, trigger_event_id,
                        error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?,
                              NULL, ?, ?)
                    """,
                    (
                        instance_id,
                        template_id,
                        template_version,
                        "template" if template_id is not None else "ad_hoc",
                        kind,
                        definition_schema_version,
                        name,
                        self._json(definition),
                        self._json(input_data or {}),
                        self._json({}),
                        len(work_items),
                        WorkflowInstanceStatus.queued.value,
                        cause_type,
                        trigger_binding_id,
                        trigger_event_id,
                        now,
                        now,
                    ),
                )
                source = "template" if template_id is not None else "ad_hoc"
                if enqueue_created_event:
                    self._insert_internal_event_row(
                        connection,
                        TriggerEventInput(
                            source_type="internal",
                            event_type="workflow.instance.created",
                            event_version=1,
                            source_key=instance_id,
                            dedup_key=(
                                f"workflow-instance-created:{instance_id}"
                            ),
                            payload={
                                "workflow_instance_id": instance_id,
                                "template_id": template_id,
                                "template_version": template_version,
                                "source": source,
                                "kind": kind,
                                "cause_type": cause_type,
                                "status": WorkflowInstanceStatus.queued.value,
                                "revision": 0,
                                "trigger_binding_id": trigger_binding_id,
                                "trigger_event_id": trigger_event_id,
                            },
                        ),
                    )
                connection.executemany(
                    """
                    INSERT INTO work_items (
                        id, workflow_instance_id, logical_key,
                        activation_number, executor_kind, spec_json, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            self.work_item_id(
                                instance_id,
                                item.logical_key,
                                item.activation_number,
                            ),
                            instance_id,
                            item.logical_key,
                            item.activation_number,
                            item.executor_kind,
                            self._json(item.spec),
                            TaskInstanceStatus.pending.value,
                            now,
                            now,
                        )
                        for item in work_items
                    ],
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                if trigger_binding_id and trigger_event_id:
                    existing = connection.execute(
                        """
                        SELECT id FROM workflow_instances
                        WHERE trigger_binding_id = ? AND trigger_event_id = ?
                        """,
                        (trigger_binding_id, trigger_event_id),
                    ).fetchone()
                    if existing is not None:
                        return str(existing["id"]), False
                raise
        return instance_id, True

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT workflow_instances.*,
                    (
                        SELECT COUNT(*) FROM work_items
                        WHERE workflow_instance_id = workflow_instances.id
                          AND status IN ('succeeded', 'failed', 'cancelled',
                                         'interrupted', 'blocked')
                    ) AS completed_work_item_count
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
                    SELECT COUNT(*) FROM work_items
                    WHERE workflow_instance_id = workflow_instances.id
                      AND status IN ('succeeded', 'failed', 'cancelled',
                                     'interrupted', 'blocked')
                ) AS completed_work_item_count
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

    def has_active_instance_for_template(self, template_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM workflow_instances
                WHERE template_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (template_id,),
            ).fetchone()
        return row is not None

    def list_queued_instance_ids(self) -> list[str]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id FROM workflow_instances
                WHERE status = ? ORDER BY created_at, id
                """,
                (WorkflowInstanceStatus.queued.value,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def find_instance_by_trigger(
        self,
        binding_id: str,
        event_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id FROM workflow_instances
                WHERE trigger_binding_id = ? AND trigger_event_id = ?
                """,
                (binding_id, event_id),
            ).fetchone()
        return self.get_instance(str(row["id"])) if row is not None else None

    def list_work_items(self, instance_id: str) -> list[dict[str, Any]]:
        self.get_instance(instance_id)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM work_items
                WHERE workflow_instance_id = ? ORDER BY rowid
                """,
                (instance_id,),
            ).fetchall()
        return [self._work_item_dict(row) for row in rows]

    def get_work_item(
        self,
        instance_id: str,
        logical_key: str,
        activation_number: int = 1,
    ) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM work_items
                WHERE workflow_instance_id = ? AND logical_key = ?
                  AND activation_number = ?
                """,
                (instance_id, logical_key, activation_number),
            ).fetchone()
        if row is None:
            raise WorkflowInstanceNotFoundError(
                f"work item {logical_key!r} activation {activation_number} "
                f"not found in workflow instance {instance_id}"
            )
        return self._work_item_dict(row)

    def set_instance_status(
        self,
        instance_id: str,
        status: WorkflowInstanceStatus,
        *,
        error: str | None = None,
        internal_event: TriggerEventInput | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            previous = connection.execute(
                "SELECT status FROM workflow_instances WHERE id = ?",
                (instance_id,),
            ).fetchone()
            cursor = connection.execute(
                """
                UPDATE workflow_instances
                SET status = ?, error = ?, revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (status.value, error, now, instance_id),
            )
            if cursor.rowcount == 0:
                raise WorkflowInstanceNotFoundError(
                    f"workflow instance not found: {instance_id}"
                )
            if internal_event is not None:
                self._insert_internal_event_row(connection, internal_event)
            connection.commit()

    def set_work_item_status(
        self,
        instance_id: str,
        logical_key: str,
        status: TaskInstanceStatus,
        *,
        activation_number: int = 1,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE work_items
                SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE workflow_instance_id = ? AND logical_key = ?
                  AND activation_number = ?
                """,
                (
                    status.value,
                    error_code,
                    error_message,
                    now,
                    instance_id,
                    logical_key,
                    activation_number,
                ),
            )
            if cursor.rowcount == 0:
                raise WorkflowInstanceNotFoundError(
                    f"work item {logical_key!r} activation {activation_number} "
                    f"not found in workflow instance {instance_id}"
                )
            connection.commit()

    def set_work_item_session(
        self,
        instance_id: str,
        logical_key: str,
        session_id: str,
        *,
        activation_number: int = 1,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE work_items SET provider_session_id = ?, updated_at = ?
                WHERE workflow_instance_id = ? AND logical_key = ?
                  AND activation_number = ?
                """,
                (session_id, now, instance_id, logical_key, activation_number),
            )
            connection.commit()

    def set_work_item_output(
        self,
        instance_id: str,
        logical_key: str,
        output: str | None,
        *,
        activation_number: int = 1,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE work_items SET final_output = ?, updated_at = ?
                WHERE workflow_instance_id = ? AND logical_key = ?
                  AND activation_number = ?
                """,
                (output, now, instance_id, logical_key, activation_number),
            )
            connection.commit()

    def start_attempt(
        self,
        instance_id: str,
        logical_key: str,
        activation_number: int = 1,
    ) -> tuple[str, int]:
        attempt_id = uuid4().hex
        now = utc_now().isoformat()
        work_item_id = self.work_item_id(
            instance_id, logical_key, activation_number
        )
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT attempt_count FROM work_items WHERE id = ?",
                (work_item_id,),
            ).fetchone()
            if row is None:
                raise WorkflowInstanceNotFoundError(
                    f"work item {logical_key!r} not found in instance {instance_id}"
                )
            attempt_number = int(row["attempt_count"]) + 1
            connection.execute(
                """
                UPDATE work_items SET attempt_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempt_number, now, work_item_id),
            )
            connection.execute(
                """
                INSERT INTO execution_attempts (
                    id, work_item_id, attempt_number, status, started_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (attempt_id, work_item_id, attempt_number, "running", now),
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
        work_item_id: str | None = None,
        attempt_id: str | None = None,
        provider: str | None = None,
    ) -> EventRecord:
        occurred_at = utc_now()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO workflow_events (
                    workflow_instance_id, work_item_id, execution_attempt_id,
                    provider, kind, occurred_at,
                    summary, payload_json, raw_event_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    work_item_id,
                    attempt_id,
                    provider,
                    event.kind.value,
                    occurred_at.isoformat(),
                    event.summary,
                    self._json(event.payload),
                    event.raw_event_type,
                ),
            )
            event_id = int(cursor.lastrowid)
            connection.commit()
        return EventRecord(
            event_id=event_id,
            workflow_instance_id=instance_id,
            work_item_id=work_item_id,
            execution_attempt_id=attempt_id,
            provider=provider,
            kind=event.kind,
            occurred_at=occurred_at,
            summary=event.summary,
            payload=event.payload,
            raw_event_type=event.raw_event_type,
        )

    def list_events(
        self,
        instance_id: str,
        after_id: int = 0,
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
                work_item_id=row["work_item_id"],
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
        logical_key: str,
        attempt_id: str,
        provider: str,
        provider_request_id: str,
        request: dict[str, Any],
        activation_number: int = 1,
    ) -> dict[str, Any]:
        approval_id = uuid4().hex
        now = utc_now().isoformat()
        work_item_id = self.work_item_id(
            instance_id, logical_key, activation_number
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO workflow_approvals (
                    id, workflow_instance_id, work_item_id,
                    execution_attempt_id, provider,
                    provider_request_id, status, request_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    instance_id,
                    work_item_id,
                    attempt_id,
                    provider,
                    provider_request_id,
                    ApprovalStatus.pending.value,
                    self._json(request),
                    now,
                ),
            )
            self._insert_internal_event_row(
                connection,
                TriggerEventInput(
                    source_type="internal",
                    event_type="approval.updated",
                    event_version=1,
                    source_key=approval_id,
                    dedup_key=f"approval-updated:{approval_id}:pending",
                    payload={
                        "approval_id": approval_id,
                        "workflow_instance_id": instance_id,
                        "work_item_id": work_item_id,
                        "status": ApprovalStatus.pending.value,
                        "decided_by": None,
                        "reason": None,
                    },
                ),
            )
            connection.commit()
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM workflow_approvals WHERE id = ?",
                (approval_id,),
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
                """
                SELECT workflow_instance_id, work_item_id, status
                FROM workflow_approvals WHERE id = ?
                """,
                (approval_id,),
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
            self._insert_internal_event_row(
                connection,
                TriggerEventInput(
                    source_type="internal",
                    event_type="approval.updated",
                    event_version=1,
                    source_key=approval_id,
                    dedup_key=f"approval-updated:{approval_id}:{status.value}",
                    payload={
                        "approval_id": approval_id,
                        "workflow_instance_id": str(
                            row["workflow_instance_id"]
                        ),
                        "work_item_id": str(row["work_item_id"]),
                        "status": status.value,
                        "decided_by": decided_by,
                        "reason": reason,
                    },
                ),
            )
            connection.commit()
        return self.get_approval(approval_id)

    def reject_pending_approvals_for_instance(
        self,
        instance_id: str,
        *,
        decided_by: str,
        reason: str,
    ) -> int:
        return self.reject_pending_approvals_for_instance_with_ids(
            instance_id,
            decided_by=decided_by,
            reason=reason,
        )["count"]

    def reject_pending_approvals_for_instance_with_ids(
        self,
        instance_id: str,
        *,
        decided_by: str,
        reason: str,
    ) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, workflow_instance_id, work_item_id
                FROM workflow_approvals
                WHERE workflow_instance_id = ? AND status = ?
                ORDER BY created_at
                """,
                (instance_id, ApprovalStatus.pending.value),
            ).fetchall()
            approval_ids = [str(row["id"]) for row in rows]
            if approval_ids:
                connection.execute(
                    """
                    UPDATE workflow_approvals
                    SET status = ?, decided_by = ?, reason = ?, decided_at = ?
                    WHERE workflow_instance_id = ? AND status = ?
                    """,
                    (
                        ApprovalStatus.rejected.value,
                        decided_by,
                        reason,
                        utc_now().isoformat(),
                        instance_id,
                        ApprovalStatus.pending.value,
                    ),
                )
                for row in rows:
                    self._insert_internal_event_row(
                        connection,
                        TriggerEventInput(
                            source_type="internal",
                            event_type="approval.updated",
                            event_version=1,
                            source_key=str(row["id"]),
                            dedup_key=(
                                "approval-updated:"
                                f"{str(row['id'])}:rejected"
                            ),
                            payload={
                                "approval_id": str(row["id"]),
                                "workflow_instance_id": str(
                                    row["workflow_instance_id"]
                                ),
                                "work_item_id": str(row["work_item_id"]),
                                "status": ApprovalStatus.rejected.value,
                                "decided_by": decided_by,
                                "reason": reason,
                            },
                        ),
                    )
            connection.commit()
        return {"count": len(approval_ids), "approval_ids": approval_ids}

    def list_pending_approval_ids_for_recovery(self) -> list[str]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT workflow_approvals.id
                FROM workflow_approvals
                JOIN workflow_instances
                  ON workflow_instances.id = workflow_approvals.workflow_instance_id
                WHERE workflow_approvals.status = ?
                  AND workflow_instances.status = ?
                ORDER BY workflow_approvals.created_at
                """,
                (
                    ApprovalStatus.pending.value,
                    WorkflowInstanceStatus.running.value,
                ),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def recover_stale(self) -> dict[str, int]:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            work_item_cursor = connection.execute(
                """
                UPDATE work_items SET status = ?, updated_at = ?
                WHERE workflow_instance_id IN (
                    SELECT id FROM workflow_instances WHERE status = ?
                )
                AND status NOT IN (?, ?, ?, ?, ?)
                """,
                (
                    TaskInstanceStatus.interrupted.value,
                    now,
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
            recovery_approvals = connection.execute(
                """
                SELECT id, workflow_instance_id, work_item_id
                FROM workflow_approvals
                WHERE status = ? AND workflow_instance_id IN (
                    SELECT id FROM workflow_instances WHERE status = ?
                )
                ORDER BY created_at
                """,
                (
                    ApprovalStatus.pending.value,
                    WorkflowInstanceStatus.running.value,
                ),
            ).fetchall()
            connection.execute(
                """
                UPDATE workflow_approvals
                SET status = ?, decided_by = ?, reason = ?, decided_at = ?
                WHERE status = ? AND workflow_instance_id IN (
                    SELECT id FROM workflow_instances WHERE status = ?
                )
                """,
                (
                    ApprovalStatus.rejected.value,
                    "system:recovery",
                    "execution was interrupted during service recovery",
                    now,
                    ApprovalStatus.pending.value,
                    WorkflowInstanceStatus.running.value,
                ),
            )
            for row in recovery_approvals:
                self._insert_internal_event_row(
                    connection,
                    TriggerEventInput(
                        source_type="internal",
                        event_type="approval.updated",
                        event_version=1,
                        source_key=str(row["id"]),
                        dedup_key=(
                            f"approval-updated:{str(row['id'])}:rejected"
                        ),
                        payload={
                            "approval_id": str(row["id"]),
                            "workflow_instance_id": str(
                                row["workflow_instance_id"]
                            ),
                            "work_item_id": str(row["work_item_id"]),
                            "status": ApprovalStatus.rejected.value,
                            "decided_by": "system:recovery",
                            "reason": (
                                "execution was interrupted during "
                                "service recovery"
                            ),
                        },
                    ),
                )
            recovery_instances = connection.execute(
                """
                SELECT id, revision FROM workflow_instances
                WHERE status = ?
                """,
                (WorkflowInstanceStatus.running.value,),
            ).fetchall()
            instance_cursor = connection.execute(
                """
                UPDATE workflow_instances
                SET status = ?, revision = revision + 1, updated_at = ?
                WHERE status = ?
                """,
                (
                    WorkflowInstanceStatus.interrupted.value,
                    now,
                    WorkflowInstanceStatus.running.value,
                ),
            )
            for row in recovery_instances:
                self._insert_internal_event_row(
                    connection,
                    TriggerEventInput(
                        source_type="internal",
                        event_type="workflow.instance.status_changed",
                        event_version=1,
                        source_key=str(row["id"]),
                        dedup_key=(
                            "workflow-instance-status:"
                            f"{str(row['id'])}:interrupted:"
                            f"{int(row['revision']) + 1}"
                        ),
                        payload={
                            "workflow_instance_id": str(row["id"]),
                            "old_status": WorkflowInstanceStatus.running.value,
                            "new_status": (
                                WorkflowInstanceStatus.interrupted.value
                            ),
                            "revision": int(row["revision"]) + 1,
                            "error": (
                                "execution was interrupted during service "
                                "recovery"
                            ),
                        },
                    ),
                )
            connection.commit()
        return {
            "instances": instance_cursor.rowcount,
            "work_items": work_item_cursor.rowcount,
            "attempts": attempt_cursor.rowcount,
        }

    @staticmethod
    def _instance_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "template_id": row["template_id"],
            "template_version": row["template_version"],
            "source": str(row["source"]),
            "kind": str(row["kind"]),
            "definition_schema_version": int(row["definition_schema_version"]),
            "name": str(row["name"]),
            "work_item_count": int(row["work_item_count"]),
            "completed_work_item_count": int(row["completed_work_item_count"]),
            "status": str(row["status"]),
            "cause_type": str(row["cause_type"]),
            "trigger_binding_id": row["trigger_binding_id"],
            "trigger_event_id": row["trigger_event_id"],
            "revision": int(row["revision"]),
            "error": row["error"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @classmethod
    def _instance_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        return {
            **cls._instance_summary(row),
            "definition": json.loads(row["definition_json"]),
            "input": json.loads(row["input_json"]),
            "runtime_state": json.loads(row["runtime_state_json"]),
        }

    @staticmethod
    def _template_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "version": int(row["version"]),
            "kind": str(row["kind"]),
            "definition_schema_version": int(row["definition_schema_version"]),
            "name": str(row["name"]),
            "work_item_count": int(row["work_item_count"]),
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
    def _work_item_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "workflow_instance_id": str(row["workflow_instance_id"]),
            "logical_key": str(row["logical_key"]),
            "activation_number": int(row["activation_number"]),
            "executor_kind": str(row["executor_kind"]),
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
            "work_item_id": str(row["work_item_id"]),
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
