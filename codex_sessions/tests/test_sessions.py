import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from codex_sessions.main import create_app
from codex_sessions.sessions import CodexSessionStore


class CodexSessionStoreTests(unittest.TestCase):
    def test_reads_all_sessions_from_sqlite_and_prefers_custom_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_home = Path(temporary_directory)
            database = codex_home / "state_5.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    title TEXT,
                    first_user_message TEXT,
                    archived INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                INSERT INTO threads VALUES
                    ('old', NULL, '旧会话', '旧消息', 0, 100),
                    ('archived', NULL, '归档会话', '归档消息', 1, 200),
                    ('new', '自定义名称', '自动标题', '新消息', 0, 300),
                    ('unnamed', '', '', '', 0, 50);
                """
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                ("long", None, "第一行\n" + "x" * 220, "长消息", 0, 250),
            )
            connection.commit()
            connection.close()

            store = CodexSessionStore(codex_home=codex_home)

            self.assertEqual(
                [session.model_dump() for session in store.list_sessions()],
                [
                    {"name": "自定义名称", "id": "new"},
                    {"name": "第一行 " + "x" * 195 + "…", "id": "long"},
                    {"name": "归档会话", "id": "archived"},
                    {"name": "旧会话", "id": "old"},
                    {"name": "未命名会话", "id": "unnamed"},
                ],
            )
            self.assertEqual(
                [session.id for session in store.list_sessions(include_archived=False)],
                ["new", "long", "old", "unnamed"],
            )

    def test_falls_back_to_session_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_home = Path(temporary_directory)
            records = [
                {"id": "first", "thread_name": "第一个会话"},
                {"id": "second", "thread_name": "第二个会话"},
                {"id": "first", "thread_name": "第一个会话（已更新）"},
            ]
            with (codex_home / "session_index.jsonl").open("w", encoding="utf-8") as index_file:
                for record in records:
                    index_file.write(json.dumps(record, ensure_ascii=False) + "\n")

            sessions = CodexSessionStore(codex_home=codex_home).list_sessions()

            self.assertEqual(
                [session.model_dump() for session in sessions],
                [
                    {"name": "第一个会话（已更新）", "id": "first"},
                    {"name": "第二个会话", "id": "second"},
                ],
            )

    def test_latest_index_name_overrides_stale_database_title(self) -> None:
        session_id = "019f89ae-f060-7e50-a870-2a493c1eb193"
        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_home = Path(temporary_directory)
            database = codex_home / "state_5.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    archived INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?)",
                (session_id, "错误的数据库标题", 0, 100),
            )
            connection.commit()
            connection.close()

            records = [
                {"id": session_id, "thread_name": "M12"},
                {"id": session_id, "thread_name": "M12 (2)"},
            ]
            with (codex_home / "session_index.jsonl").open("w", encoding="utf-8") as index_file:
                for record in records:
                    index_file.write(json.dumps(record, ensure_ascii=False) + "\n")

            sessions = CodexSessionStore(codex_home=codex_home).list_sessions()

            self.assertEqual(
                [session.model_dump() for session in sessions],
                [{"name": "M12 (2)", "id": session_id}],
            )


class ApiTests(unittest.TestCase):
    def test_sessions_endpoint_returns_only_name_and_id(self) -> None:
        class FakeStore:
            def list_sessions(self, include_archived: bool = True):
                self.include_archived = include_archived
                return [{"name": "测试会话", "id": "session-id"}]

        store = FakeStore()
        with TestClient(create_app(store=store)) as client:
            response = client.get("/sessions?include_archived=false")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"name": "测试会话", "id": "session-id"}])
        self.assertFalse(store.include_archived)


if __name__ == "__main__":
    unittest.main()
