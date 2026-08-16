from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any
from uuid import uuid4

from multi_agent.domain.errors import (
    TriggerBindingConflictError,
    TriggerBindingNotFoundError,
    TriggerEventNotFoundError,
    TriggerEventProcessingError,
)
from multi_agent.domain.models import (
    TriggerBindingDefinition,
    TriggerDeliveryStatus,
    TriggerEventInput,
    TriggerEventStatus,
    utc_now,
)


class SQLiteTriggerStoreMixin:
    """SQLite persistence operations for event ingress and trigger delivery."""

    def create_trigger_binding(
        self,
        binding: TriggerBindingDefinition,
    ) -> dict[str, Any]:
        self.get_template(binding.template_id)
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO trigger_bindings (
                        id, name, source_type, event_type, event_version, source_key,
                        template_id, enabled, source_config_json, event_filter_json,
                        input_mapping_json, concurrency_policy,
                        created_at, updated_at, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        binding.id,
                        binding.name,
                        binding.source_type,
                        binding.event_type,
                        binding.event_version,
                        binding.source_key,
                        binding.template_id,
                        int(binding.enabled),
                        self._json(binding.source_config),
                        self._json(binding.event_filter),
                        self._json(binding.input_mapping),
                        binding.concurrency_policy.value,
                        now,
                        now,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise TriggerBindingConflictError(
                    f"trigger binding {binding.id!r} already exists"
                ) from exc
        return self.get_trigger_binding(binding.id)

    def get_trigger_binding(
        self,
        binding_id: str,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        query = "SELECT * FROM trigger_bindings WHERE id = ?"
        if not include_archived:
            query += " AND archived_at IS NULL"
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(query, (binding_id,)).fetchone()
        if row is None:
            raise TriggerBindingNotFoundError(
                f"trigger binding not found: {binding_id}"
            )
        return self._trigger_binding_dict(row)

    def list_trigger_bindings(
        self,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM trigger_bindings"
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        query += " ORDER BY updated_at DESC, id DESC"
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query).fetchall()
        return [self._trigger_binding_dict(row) for row in rows]

    def update_trigger_binding(
        self,
        binding_id: str,
        binding: TriggerBindingDefinition,
    ) -> dict[str, Any]:
        if binding.id != binding_id:
            raise TriggerBindingConflictError(
                "trigger binding body id must match the path id"
            )
        self.get_template(binding.template_id)
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            try:
                cursor = connection.execute(
                    """
                    UPDATE trigger_bindings
                    SET name = ?, source_type = ?, event_type = ?, event_version = ?,
                        source_key = ?,
                        template_id = ?, enabled = ?, source_config_json = ?,
                        event_filter_json = ?,
                        input_mapping_json = ?, concurrency_policy = ?,
                        updated_at = ?
                    WHERE id = ? AND archived_at IS NULL
                    """,
                    (
                        binding.name,
                        binding.source_type,
                        binding.event_type,
                        binding.event_version,
                        binding.source_key,
                        binding.template_id,
                        int(binding.enabled),
                        self._json(binding.source_config),
                        self._json(binding.event_filter),
                        self._json(binding.input_mapping),
                        binding.concurrency_policy.value,
                        now,
                        binding_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TriggerBindingConflictError(
                    "trigger binding conflicts with an existing active "
                    f"source key for source_type={binding.source_type!r}"
                ) from exc
            if cursor.rowcount == 0:
                raise TriggerBindingNotFoundError(
                    f"trigger binding not found: {binding_id}"
                )
            connection.commit()
        return self.get_trigger_binding(binding_id)

    def set_trigger_binding_enabled(
        self,
        binding_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        if enabled:
            binding = self.get_trigger_binding(binding_id)
            self.get_template(binding["template_id"])
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE trigger_bindings SET enabled = ?, updated_at = ?
                WHERE id = ? AND archived_at IS NULL
                """,
                (int(enabled), utc_now().isoformat(), binding_id),
            )
            if cursor.rowcount == 0:
                raise TriggerBindingNotFoundError(
                    f"trigger binding not found: {binding_id}"
                )
            connection.commit()
        return self.get_trigger_binding(binding_id)

    def archive_trigger_binding(self, binding_id: str) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE trigger_bindings
                SET enabled = 0, archived_at = COALESCE(archived_at, ?),
                    updated_at = ? WHERE id = ?
                """,
                (now, now, binding_id),
            )
            if cursor.rowcount == 0:
                raise TriggerBindingNotFoundError(
                    f"trigger binding not found: {binding_id}"
                )
            connection.commit()
        return self.get_trigger_binding(binding_id, include_archived=True)

    def find_trigger_binding_by_source_key(
        self,
        source_type: str,
        source_key: str,
        *,
        exclude_id: str | None = None,
    ) -> dict[str, Any] | None:
        query = """
            SELECT * FROM trigger_bindings
            WHERE source_type = ? AND source_key = ? AND archived_at IS NULL
        """
        params: list[Any] = [source_type, source_key]
        if exclude_id is not None:
            query += " AND id != ?"
            params.append(exclude_id)
        query += " ORDER BY created_at, id LIMIT 1"
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return self._trigger_binding_dict(row) if row is not None else None

    def list_matching_trigger_bindings(
        self,
        *,
        source_type: str,
        event_type: str,
        event_version: int,
        source_key: str | None,
    ) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM trigger_bindings
                WHERE enabled = 1 AND archived_at IS NULL
                  AND source_type = ? AND event_type = ?
                  AND event_version = ?
                  AND (source_key IS NULL OR source_key = ?)
                ORDER BY created_at, id
                """,
                (source_type, event_type, event_version, source_key),
            ).fetchall()
        return [self._trigger_binding_dict(row) for row in rows]

    def create_trigger_event(
        self,
        event: TriggerEventInput,
    ) -> tuple[dict[str, Any], bool]:
        event_id = uuid4().hex
        now = utc_now().isoformat()
        payload_json = self._json(event.payload)
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO trigger_events (
                        id, source_type, event_type, event_version,
                        source_key, dedup_key,
                        payload_json, status, error, received_at, processed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
                    """,
                    (
                        event_id,
                        event.source_type,
                        event.event_type,
                        event.event_version,
                        event.source_key,
                        event.dedup_key,
                        payload_json,
                        TriggerEventStatus.received.value,
                        now,
                    ),
                )
                connection.commit()
                created = True
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT * FROM trigger_events
                    WHERE source_type = ? AND dedup_key = ?
                    """,
                    (event.source_type, event.dedup_key),
                ).fetchone()
                if row is None:
                    raise
                if (
                    row["event_type"] != event.event_type
                    or int(row["event_version"]) != event.event_version
                    or row["source_key"] != event.source_key
                    or row["payload_json"] != payload_json
                ):
                    raise TriggerEventProcessingError(
                        "dedup_key was already used with different event data"
                    )
                event_id = str(row["id"])
                created = False
        return self.get_trigger_event(event_id), created

    def get_trigger_event(self, event_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM trigger_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise TriggerEventNotFoundError(f"trigger event not found: {event_id}")
        return self._trigger_event_dict(row)

    def list_trigger_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM trigger_events
                ORDER BY received_at DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._trigger_event_dict(row) for row in rows]

    def set_trigger_event_status(
        self,
        event_id: str,
        status: TriggerEventStatus,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        processed_at = (
            utc_now().isoformat()
            if status in {TriggerEventStatus.processed, TriggerEventStatus.failed}
            else None
        )
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE trigger_events
                SET status = ?, error = ?, processed_at = ? WHERE id = ?
                """,
                (status.value, error, processed_at, event_id),
            )
            if cursor.rowcount == 0:
                raise TriggerEventNotFoundError(
                    f"trigger event not found: {event_id}"
                )
            connection.commit()
        return self.get_trigger_event(event_id)

    def retry_trigger_event(self, event_id: str) -> dict[str, Any]:
        self.get_trigger_event(event_id)
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE trigger_deliveries
                SET status = ?, workflow_instance_id = NULL, reason = NULL,
                    error = NULL, updated_at = ?
                WHERE trigger_event_id = ? AND status = ?
                """,
                (
                    TriggerDeliveryStatus.pending.value,
                    now,
                    event_id,
                    TriggerDeliveryStatus.failed.value,
                ),
            )
            connection.execute(
                """
                UPDATE trigger_events
                SET status = ?, error = NULL, processed_at = NULL WHERE id = ?
                """,
                (TriggerEventStatus.received.value, event_id),
            )
            connection.commit()
        return self.get_trigger_event(event_id)

    def create_trigger_delivery(
        self,
        *,
        event_id: str,
        binding_id: str,
    ) -> dict[str, Any]:
        delivery_id = uuid4().hex
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            binding_row = connection.execute(
                "SELECT * FROM trigger_bindings WHERE id = ?",
                (binding_id,),
            ).fetchone()
            if binding_row is None:
                raise TriggerBindingNotFoundError(
                    f"trigger binding not found: {binding_id}"
                )
            binding_snapshot = self._trigger_binding_dict(binding_row)
            connection.execute(
                """
                INSERT OR IGNORE INTO trigger_deliveries (
                    id, trigger_event_id, trigger_binding_id,
                    binding_snapshot_json, workflow_instance_id,
                    status, reason, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?)
                """,
                (
                    delivery_id,
                    event_id,
                    binding_id,
                    self._json(binding_snapshot),
                    TriggerDeliveryStatus.pending.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM trigger_deliveries
                WHERE trigger_event_id = ? AND trigger_binding_id = ?
                """,
                (event_id, binding_id),
            ).fetchone()
            connection.commit()
        return self._trigger_delivery_dict(row)

    def get_trigger_delivery(self, delivery_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM trigger_deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise TriggerEventProcessingError(
                f"trigger delivery not found: {delivery_id}"
            )
        return self._trigger_delivery_dict(row)

    def list_trigger_deliveries(
        self,
        event_id: str,
    ) -> list[dict[str, Any]]:
        self.get_trigger_event(event_id)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM trigger_deliveries
                WHERE trigger_event_id = ? ORDER BY created_at, id
                """,
                (event_id,),
            ).fetchall()
        return [self._trigger_delivery_dict(row) for row in rows]

    def list_pending_trigger_deliveries(self) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM trigger_deliveries
                WHERE status = ? ORDER BY created_at, id
                """,
                (TriggerDeliveryStatus.pending.value,),
            ).fetchall()
        return [self._trigger_delivery_dict(row) for row in rows]

    def finish_trigger_delivery(
        self,
        delivery_id: str,
        status: TriggerDeliveryStatus,
        *,
        instance_id: str | None = None,
        reason: str | None = None,
        error: str | None = None,
        internal_event: TriggerEventInput | None = None,
    ) -> dict[str, Any]:
        if status == TriggerDeliveryStatus.pending:
            raise ValueError("a completed trigger delivery cannot be pending")
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE trigger_deliveries
                SET status = ?, workflow_instance_id = ?, reason = ?, error = ?,
                    updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    status.value,
                    instance_id,
                    reason,
                    error,
                    utc_now().isoformat(),
                    delivery_id,
                    TriggerDeliveryStatus.pending.value,
                ),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT id FROM trigger_deliveries WHERE id = ?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    raise TriggerEventProcessingError(
                        f"trigger delivery not found: {delivery_id}"
                    )
            if internal_event is not None:
                self._insert_internal_event_row(connection, internal_event)
            connection.commit()
        return self.get_trigger_delivery(delivery_id)

    def get_trigger_source_state(
        self,
        source_type: str,
        source_key: str,
    ) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM trigger_source_state
                WHERE source_type = ? AND source_key = ?
                """,
                (source_type, source_key),
            ).fetchone()
        if row is None:
            return None
        return {
            "source_type": str(row["source_type"]),
            "source_key": str(row["source_key"]),
            "cursor": json.loads(row["cursor_json"]),
            "updated_at": str(row["updated_at"]),
        }
    def set_trigger_source_state(
        self,
        source_type: str,
        source_key: str,
        cursor: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO trigger_source_state (
                    source_type, source_key, cursor_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_type, source_key) DO UPDATE SET
                    cursor_json = excluded.cursor_json,
                    updated_at = excluded.updated_at
                """,
                (
                    source_type,
                    source_key,
                    self._json(cursor),
                    utc_now().isoformat(),
                ),
            )
            connection.commit()
        result = self.get_trigger_source_state(source_type, source_key)
        assert result is not None
        return result

    @staticmethod
    def _trigger_binding_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "source_type": str(row["source_type"]),
            "event_type": str(row["event_type"]),
            "event_version": int(row["event_version"]),
            "source_key": row["source_key"],
            "template_id": str(row["template_id"]),
            "enabled": bool(row["enabled"]),
            "source_config": json.loads(row["source_config_json"]),
            "event_filter": json.loads(row["event_filter_json"]),
            "input_mapping": json.loads(row["input_mapping_json"]),
            "concurrency_policy": str(row["concurrency_policy"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "archived_at": row["archived_at"],
        }

    @staticmethod
    def _trigger_event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "source_type": str(row["source_type"]),
            "event_type": str(row["event_type"]),
            "event_version": int(row["event_version"]),
            "source_key": row["source_key"],
            "dedup_key": str(row["dedup_key"]),
            "payload": json.loads(row["payload_json"]),
            "status": str(row["status"]),
            "error": row["error"],
            "received_at": str(row["received_at"]),
            "processed_at": row["processed_at"],
        }

    @staticmethod
    def _trigger_delivery_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "trigger_event_id": str(row["trigger_event_id"]),
            "trigger_binding_id": str(row["trigger_binding_id"]),
            "binding_snapshot": json.loads(row["binding_snapshot_json"]),
            "workflow_instance_id": row["workflow_instance_id"],
            "status": str(row["status"]),
            "reason": row["reason"],
            "error": row["error"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
