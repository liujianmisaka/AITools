from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from multi_agent.domain.models import TriggerEventInput, utc_now


class SQLiteOutboxStoreMixin:
    """Durable outbox for application-owned internal events."""

    def _insert_internal_event_row(
        self,
        connection: sqlite3.Connection,
        event: TriggerEventInput,
    ) -> None:
        now = utc_now().isoformat()
        connection.execute(
            """
            INSERT OR IGNORE INTO internal_event_outbox (
                source_type, event_type, event_version, source_key,
                dedup_key, payload_json, status, attempts, created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                event.source_type,
                event.event_type,
                event.event_version,
                event.source_key,
                event.dedup_key,
                self._json(event.payload),
                "pending",
                now,
                now,
            ),
        )

    def enqueue_internal_event(
        self,
        event: TriggerEventInput,
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            existing = connection.execute(
                """
                SELECT * FROM internal_event_outbox
                WHERE source_type = ? AND dedup_key = ?
                """,
                (event.source_type, event.dedup_key),
            ).fetchone()
            if existing is not None:
                return self._outbox_row_dict(existing)
            cursor = connection.execute(
                """
                INSERT INTO internal_event_outbox (
                    source_type, event_type, event_version, source_key,
                    dedup_key, payload_json, status, attempts, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    event.source_type,
                    event.event_type,
                    event.event_version,
                    event.source_key,
                    event.dedup_key,
                    self._json(event.payload),
                    "pending",
                    now,
                    now,
                ),
            )
            connection.commit()
            outbox_id = int(cursor.lastrowid)
        return self.get_internal_event_outbox(outbox_id)

    def get_internal_event_outbox(self, outbox_id: int) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM internal_event_outbox WHERE id = ?",
                (outbox_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"internal event outbox row not found: {outbox_id}")
        return self._outbox_row_dict(row)

    def list_recoverable_internal_events(
        self,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM internal_event_outbox
                WHERE status = 'pending'
                   OR (status = 'failed' AND attempts < 5)
                ORDER BY id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._outbox_row_dict(row) for row in rows]

    def mark_internal_event_published(self, outbox_id: int) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE internal_event_outbox
                SET status = 'published', error = NULL, updated_at = ?,
                    published_at = ?
                WHERE id = ? AND status IN ('pending', 'failed')
                """,
                (now, now, outbox_id),
            )
            connection.commit()

    def mark_internal_event_failed(
        self,
        outbox_id: int,
        error: str,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE internal_event_outbox
                SET status = 'failed', attempts = attempts + 1, error = ?,
                    updated_at = ?
                WHERE id = ? AND status IN ('pending', 'failed')
                """,
                (error, now, outbox_id),
            )
            connection.commit()

    @staticmethod
    def _outbox_row_dict(row: sqlite3.Row) -> dict[str, Any]:
        import json

        return {
            "id": int(row["id"]),
            "source_type": str(row["source_type"]),
            "event_type": str(row["event_type"]),
            "event_version": int(row["event_version"]),
            "source_key": row["source_key"],
            "dedup_key": str(row["dedup_key"]),
            "payload": json.loads(row["payload_json"]),
            "status": str(row["status"]),
            "attempts": int(row["attempts"]),
            "error": row["error"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "published_at": row["published_at"],
        }
