from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from multi_agent.domain.models import (
    ApprovalStatus,
    EventKind,
    ProviderEvent,
    TaskInstanceStatus,
    TaskSpec,
    TriggerEventInput,
    WorkflowDefinition,
    WorkflowInstanceStatus,
)
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.orchestration.dag import DagOrchestrationModel
from multi_agent.domain.errors import (
    WorkflowTemplateNotFoundError,
    WorkflowTemplateVersionConflictError,
)


class SQLiteStoreTests(unittest.TestCase):
    @staticmethod
    def _template(name: str, template_id: str) -> WorkflowDefinition:
        return WorkflowDefinition(
            id=template_id,
            name=name,
            tasks=[
                TaskSpec(
                    id="one",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="one",
                )
            ],
        )

    @staticmethod
    def _create_template(store: SQLiteStore, workflow: WorkflowDefinition) -> dict:
        model = DagOrchestrationModel()
        return store.create_template(
            template_id=workflow.id,
            version=workflow.version,
            kind=model.kind,
            definition_schema_version=model.definition_schema_version,
            name=workflow.name,
            definition=workflow.model_dump(mode="json"),
            work_item_count=len(workflow.tasks),
        )

    @staticmethod
    def _create_instance(
        store: SQLiteStore,
        workflow: WorkflowDefinition,
        *,
        template: bool = False,
    ) -> str:
        model = DagOrchestrationModel()
        instance_id, created = store.create_instance(
            kind=model.kind,
            definition_schema_version=model.definition_schema_version,
            name=workflow.name,
            definition=workflow.model_dump(mode="json"),
            work_items=model.materialize_work_items(workflow),
            template_id=workflow.id if template else None,
            template_version=workflow.version if template else None,
        )
        assert created
        return instance_id

    def test_template_crud_pagination_conflict_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SQLiteStore(Path(temporary_directory) / "state.sqlite3")
            store.initialize()
            for index in range(3):
                created = self._create_template(store,
                    self._template(f"workflow {index}", f"workflow_{index}")
                )
                self.assertEqual(created["version"], 1)
                self.assertEqual(created["work_item_count"], 1)

            first_page = store.list_templates(limit=2)
            self.assertEqual(len(first_page["items"]), 2)
            self.assertIsNotNone(first_page["next_cursor"])
            second_page = store.list_templates(
                limit=2,
                cursor=first_page["next_cursor"],
            )
            self.assertEqual(len(second_page["items"]), 1)
            self.assertEqual(
                {item["id"] for item in first_page["items"] + second_page["items"]},
                {"workflow_0", "workflow_1", "workflow_2"},
            )

            original = self._template("workflow 0 updated", "workflow_0")
            next_definition = original.model_copy(update={"version": 2})
            updated = store.update_template(
                "workflow_0",
                expected_version=1,
                kind="dag",
                definition_schema_version=1,
                name=next_definition.name,
                definition=next_definition.model_dump(mode="json"),
                work_item_count=len(next_definition.tasks),
            )
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["definition"]["version"], 2)
            self.assertEqual(updated["name"], "workflow 0 updated")

            with self.assertRaises(WorkflowTemplateVersionConflictError):
                store.update_template(
                    "workflow_0",
                    expected_version=1,
                    kind="dag",
                    definition_schema_version=1,
                    name=original.name,
                    definition=original.model_dump(mode="json"),
                    work_item_count=len(original.tasks),
                )

            archived = store.archive_template("workflow_0")
            self.assertIsNotNone(archived["archived_at"])
            with self.assertRaises(WorkflowTemplateNotFoundError):
                store.get_template("workflow_0")
            self.assertEqual(
                store.get_template("workflow_0", include_archived=True)["id"],
                "workflow_0",
            )
            self.assertNotIn(
                "workflow_0",
                {item["id"] for item in store.list_templates()["items"]},
            )

    def test_fresh_database_uses_single_schema_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "state.sqlite3"
            store = SQLiteStore(database)
            store.initialize()
            with closing(sqlite3.connect(database)) as connection:
                version = connection.execute(
                    "SELECT version FROM schema_metadata WHERE id = 1"
                ).fetchone()[0]
                plan = " ".join(
                    str(value)
                    for row in connection.execute(
                        """
                        EXPLAIN QUERY PLAN
                        SELECT * FROM workflow_templates
                        WHERE archived_at IS NULL
                        ORDER BY updated_at DESC, id DESC
                        LIMIT 50
                        """
                    )
                    for value in row
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type = 'table'"
                    )
                }
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type = 'index'"
                    )
                }
            self.assertEqual(version, 5)
            self.assertIn("idx_workflow_templates_active_updated", plan)
            self.assertIn("workflow_templates", tables)
            self.assertIn("workflow_instances", tables)
            self.assertIn("work_items", tables)
            self.assertIn("trigger_events", tables)
            self.assertIn("scheduled_tasks", tables)
            self.assertIn("scheduled_task_runs", tables)
            self.assertIn("internal_event_outbox", tables)
            self.assertIn("idx_trigger_bindings_webhook_source_key", indexes)
            self.assertIn("idx_internal_event_outbox_recoverable", indexes)
            self.assertIn("idx_internal_event_outbox_published_at", indexes)
            self.assertNotIn("workflows", tables)
            self.assertNotIn("runs", tables)

    def test_outbox_failure_cannot_overwrite_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SQLiteStore(Path(temporary_directory) / "state.sqlite3")
            store.initialize()
            outbox = store.enqueue_internal_event(
                TriggerEventInput(
                    source_type="internal",
                    event_type="schedule.run.updated",
                    event_version=1,
                    source_key="task",
                    dedup_key="outbox-conditional",
                    payload={
                        "scheduled_task_id": "task",
                        "run_id": "run",
                        "status": "failed",
                        "scheduled_for": None,
                        "error": "test",
                    },
                )
            )
            store.mark_internal_event_published(outbox["id"])
            store.mark_internal_event_failed(outbox["id"], "late failure")
            self.assertEqual(
                store.get_internal_event_outbox(outbox["id"])["status"],
                "published",
            )

    def test_outbox_recoverable_query_uses_partial_index_and_purges_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "state.sqlite3"
            store = SQLiteStore(database)
            store.initialize()
            outbox = store.enqueue_internal_event(
                TriggerEventInput(
                    source_type="internal",
                    event_type="schedule.run.updated",
                    event_version=1,
                    source_key="task",
                    dedup_key="outbox-purge",
                    payload={
                        "scheduled_task_id": "task",
                        "run_id": "run",
                        "status": "failed",
                        "scheduled_for": None,
                        "error": "test",
                    },
                )
            )
            store.mark_internal_event_published(outbox["id"])
            with closing(sqlite3.connect(database)) as connection:
                plan = " ".join(
                    str(value)
                    for row in connection.execute(
                        """
                        EXPLAIN QUERY PLAN
                        SELECT * FROM internal_event_outbox
                        WHERE status = 'pending'
                           OR (status = 'failed' AND attempts < 5)
                        ORDER BY id LIMIT 100
                        """
                    )
                    for value in row
                )
                connection.execute(
                    """
                    UPDATE internal_event_outbox
                    SET published_at = '2000-01-01T00:00:00+00:00'
                    WHERE id = ?
                    """,
                    (outbox["id"],),
                )
                connection.commit()
            self.assertIn("idx_internal_event_outbox_recoverable", plan)
            self.assertEqual(store.purge_published_internal_events(3600), 1)
            self.assertNotIn(
                "outbox-purge",
                {
                    row["dedup_key"]
                    for row in store.list_recoverable_internal_events()
                },
            )

    def test_rejects_legacy_database_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        workflow_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()

            with self.assertRaisesRegex(RuntimeError, "legacy database schema"):
                SQLiteStore(database).initialize()

    def test_instance_list_paginates_filters_and_uses_created_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "state.sqlite3"
            store = SQLiteStore(database)
            store.initialize()
            template = self._template("instance template", "instance_template")
            self._create_template(store, template)
            instance_ids = [
                self._create_instance(store, template, template=True)
                for _ in range(3)
            ]
            store.set_work_item_status(
                instance_ids[-1],
                "one",
                TaskInstanceStatus.succeeded,
            )
            store.set_instance_status(
                instance_ids[-1],
                WorkflowInstanceStatus.succeeded,
            )

            first_page = store.list_instances(limit=2)
            second_page = store.list_instances(
                limit=2,
                cursor=first_page["next_cursor"],
            )
            succeeded = store.list_instances(
                status=WorkflowInstanceStatus.succeeded,
            )

            self.assertEqual(len(first_page["items"]), 2)
            self.assertEqual(len(second_page["items"]), 1)
            self.assertEqual(
                {item["id"] for item in first_page["items"] + second_page["items"]},
                set(instance_ids),
            )
            self.assertEqual(
                succeeded["items"][0]["completed_work_item_count"], 1
            )
            with closing(sqlite3.connect(database)) as connection:
                plan = " ".join(
                    str(value)
                    for row in connection.execute(
                        """
                        EXPLAIN QUERY PLAN
                        SELECT * FROM workflow_instances
                        ORDER BY created_at DESC, id DESC
                        LIMIT 50
                        """
                    )
                    for value in row
                )
            self.assertIn("idx_workflow_instances_created", plan)

    def test_rejects_incompatible_database_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE schema_metadata (
                        id INTEGER PRIMARY KEY CHECK(id = 1),
                        version INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO schema_metadata VALUES (1, 999)"
                )
                connection.commit()

            with self.assertRaisesRegex(RuntimeError, "schema version is incompatible"):
                SQLiteStore(database).initialize()

    def test_events_are_ordered_and_resume_after_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SQLiteStore(Path(temporary_directory) / "state.sqlite3")
            store.initialize()
            instance_id = self._create_instance(store,
                WorkflowDefinition(
                    name="events",
                    tasks=[
                        TaskSpec(
                            id="one",
                            provider="fake",
                            workspace_id="repo",
                            prompt_template="one",
                        )
                    ],
                )
            )
            first = store.append_event(
                instance_id=instance_id,
                event=ProviderEvent(kind=EventKind.started, summary="first"),
            )
            second = store.append_event(
                instance_id=instance_id,
                event=ProviderEvent(kind=EventKind.completed, summary="second"),
            )

            self.assertEqual(
                [event.summary for event in store.list_events(instance_id)],
                ["first", "second"],
            )
            self.assertEqual(
                [event.event_id for event in store.list_events(instance_id, first.event_id)],
                [second.event_id],
            )

    def test_recovery_marks_live_state_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SQLiteStore(Path(temporary_directory) / "state.sqlite3")
            store.initialize()
            instance_id = self._create_instance(store,
                WorkflowDefinition(
                    name="recover",
                    tasks=[
                        TaskSpec(
                            id="one",
                            provider="fake",
                            workspace_id="repo",
                            prompt_template="one",
                        )
                    ],
                )
            )
            store.set_instance_status(instance_id, WorkflowInstanceStatus.running)
            store.set_work_item_status(instance_id, "one", TaskInstanceStatus.running)
            attempt_id, _ = store.start_attempt(instance_id, "one")

            recovered = store.recover_stale()

            self.assertEqual(
                recovered,
                {"instances": 1, "work_items": 1, "attempts": 1},
            )
            self.assertEqual(store.get_instance(instance_id)["status"], "interrupted")
            self.assertEqual(
                store.get_work_item(instance_id, "one")["status"],
                "interrupted",
            )
            store.finish_attempt(attempt_id, "interrupted")

    def test_recovery_interrupts_queued_tasks_and_rejects_orphan_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SQLiteStore(Path(temporary_directory) / "state.sqlite3")
            store.initialize()
            instance_id = self._create_instance(store,
                WorkflowDefinition(
                    name="queued recovery",
                    tasks=[
                        TaskSpec(
                            id="one",
                            provider="fake",
                            workspace_id="repo",
                            prompt_template="one",
                        )
                    ],
                )
            )
            store.set_instance_status(instance_id, WorkflowInstanceStatus.running)
            attempt_id, _ = store.start_attempt(instance_id, "one")
            approval = store.create_approval(
                instance_id=instance_id,
                logical_key="one",
                attempt_id=attempt_id,
                provider="fake",
                provider_request_id="request",
                request={"tool": "write"},
            )

            recovered = store.recover_stale()

            self.assertEqual(
                recovered,
                {"instances": 1, "work_items": 1, "attempts": 1},
            )
            self.assertEqual(store.get_instance(instance_id)["status"], "interrupted")
            self.assertEqual(
                store.get_work_item(instance_id, "one")["status"],
                "interrupted",
            )
            resolved = store.get_approval(approval["id"])
            self.assertEqual(resolved["status"], ApprovalStatus.rejected.value)
            self.assertEqual(resolved["decided_by"], "system:recovery")
