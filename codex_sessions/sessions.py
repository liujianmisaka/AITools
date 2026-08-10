from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from codex_sessions.models import SessionSummary

MAX_SESSION_NAME_LENGTH = 200


class CodexSessionReadError(RuntimeError):
    """Raised when no local Codex session source can be read."""


class CodexSessionStore:
    """Read local Codex session metadata without modifying Codex state."""

    def __init__(
        self,
        codex_home: Path | str | None = None,
        state_db: Path | str | None = None,
    ) -> None:
        configured_home = codex_home or os.getenv("CODEX_HOME")
        self.codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"

        configured_db = state_db or os.getenv("CODEX_STATE_DB")
        self.state_db = Path(configured_db).expanduser() if configured_db else self.codex_home / "state_5.sqlite"
        self.session_index = self.codex_home / "session_index.jsonl"

    def list_sessions(self, include_archived: bool = True) -> list[SessionSummary]:
        """Return sessions ordered from most recently updated to oldest."""

        failures: list[str] = []

        if self.state_db.is_file():
            try:
                return self._read_sqlite(include_archived=include_archived)
            except (OSError, sqlite3.Error, ValueError) as exc:
                failures.append(f"{self.state_db}: {exc}")

        if self.session_index.is_file():
            try:
                return self._read_session_index()
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                failures.append(f"{self.session_index}: {exc}")

        detail = "; ".join(failures) if failures else "未找到 Codex 本地会话数据库或索引文件"
        raise CodexSessionReadError(detail)

    def _read_sqlite(self, include_archived: bool) -> list[SessionSummary]:
        database_uri = f"{self.state_db.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(database_uri, uri=True, timeout=2.0)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")

            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(threads)")
            }
            if "id" not in columns:
                raise ValueError("threads 表不存在或缺少 id 字段")

            name_columns = [column for column in ("name", "title", "first_user_message") if column in columns]
            if name_columns:
                raw_name_expression = "COALESCE(" + ", ".join(
                    f"NULLIF(TRIM({column}), '')" for column in name_columns
                ) + ", '未命名会话')"
            else:
                raw_name_expression = "'未命名会话'"

            single_line_name = (
                f"REPLACE(REPLACE(REPLACE({raw_name_expression}, CHAR(13), ' '), "
                "CHAR(10), ' '), CHAR(9), ' ')"
            )
            name_expression = (
                f"CASE WHEN LENGTH({single_line_name}) > {MAX_SESSION_NAME_LENGTH} "
                f"THEN RTRIM(SUBSTR({single_line_name}, 1, {MAX_SESSION_NAME_LENGTH - 1})) || '…' "
                f"ELSE {single_line_name} END"
            )

            order_columns = [
                column
                for column in ("recency_at_ms", "updated_at_ms", "updated_at", "created_at_ms", "created_at")
                if column in columns
            ]
            order_expression = ", ".join(
                f"COALESCE({column}, 0) DESC" for column in order_columns
            ) or "id DESC"

            where_clause = ""
            parameters: tuple[int, ...] = ()
            if not include_archived and "archived" in columns:
                where_clause = "WHERE archived = ?"
                parameters = (0,)

            rows = connection.execute(
                f"""
                SELECT id, {name_expression} AS display_name
                FROM threads
                {where_clause}
                ORDER BY {order_expression}
                """,
                parameters,
            ).fetchall()

        indexed_names: dict[str, str] = {}
        if self.session_index.is_file():
            try:
                indexed_names = self._read_session_index_names()
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                # The SQLite database still provides a complete, usable result if
                # the optional display-name index is temporarily unreadable.
                indexed_names = {}

        return [
            SessionSummary(
                name=self._normalize_name(indexed_names.get(str(row["id"]), row["display_name"])),
                id=str(row["id"]),
            )
            for row in rows
        ]

    def _read_session_index(self) -> list[SessionSummary]:
        indexed_names = self._read_session_index_names()
        return [
            SessionSummary(name=self._normalize_name(name), id=session_id)
            for session_id, name in reversed(indexed_names.items())
        ]

    def _read_session_index_names(self) -> dict[str, str]:
        sessions: dict[str, str] = {}
        with self.session_index.open("r", encoding="utf-8") as index_file:
            for line_number, raw_line in enumerate(index_file, start=1):
                line = raw_line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise json.JSONDecodeError(
                        f"session_index.jsonl 第 {line_number} 行无效: {exc.msg}",
                        exc.doc,
                        exc.pos,
                    ) from exc

                session_id = str(record.get("id", "")).strip()
                if not session_id:
                    raise ValueError(f"session_index.jsonl 第 {line_number} 行缺少 id")

                name = self._normalize_name(record.get("thread_name"))
                # Reinsert an existing ID so its latest index entry also controls ordering.
                sessions.pop(session_id, None)
                sessions[session_id] = name

        return sessions

    @staticmethod
    def _normalize_name(value: object) -> str:
        name = " ".join(str(value or "").split()) or "未命名会话"
        if len(name) <= MAX_SESSION_NAME_LENGTH:
            return name
        return name[: MAX_SESSION_NAME_LENGTH - 1].rstrip() + "…"
