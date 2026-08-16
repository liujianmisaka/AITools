from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from multi_agent.domain.models import (
    ScheduledTaskDefinition,
    TriggerBindingDefinition,
    WorkflowDefinition,
)
from multi_agent.main import create_app
from multi_agent.orchestration.service import OrchestrationApplicationService
from multi_agent.triggers.events import EventTypeDefinition, UnrestrictedPayload
from multi_agent.triggers.sources import EventSourceRegistry, ManualEventSource
from tests.helpers import EngineFixture


class ScheduleExtensionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = EngineFixture()
        self.client_context = TestClient(create_app(self.fixture.engine))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.fixture._temp.cleanup()

    def test_expired_one_time_task_is_recorded_failed_and_disabled(self) -> None:
        response = self.client.post(
            "/api/v1/scheduled-tasks",
            json={
                "id": "expired_one_time",
                "name": "expired one-time",
                "schedule_type": "one_time",
                "schedule": {
                    "run_at": "2000-01-01T00:00:00Z",
                    "misfire_grace_seconds": 60,
                },
                "action_type": "publish_trigger_event",
                "action": {},
                "enabled": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        task = response.json()
        self.assertFalse(task["enabled"])
        self.assertEqual(task["last_status"], "failed")
        self.assertIsNone(task["next_run_at"])
        runs = self.client.get(
            "/api/v1/scheduled-tasks/expired_one_time/runs"
        ).json()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertIn("misfire grace", runs[0]["error"])
        self.assertEqual(
            self.client.get("/health").json(), {"status": "ok"}
        )

    def test_publish_trigger_event_emits_schedule_tick_with_sequence(self) -> None:
        self.client.post(
            "/api/v1/templates",
            json={
                "id": "tick_flow",
                "name": "tick flow",
                "tasks": [
                    {
                        "id": "consume",
                        "provider": "fake",
                        "workspace_id": "repo",
                        "prompt_template": "tick",
                    }
                ],
            },
        )
        self.client.post(
            "/api/v1/triggers",
            json={
                "id": "tick_binding",
                "name": "tick binding",
                "source_type": "schedule",
                "event_type": "schedule.tick",
                "source_key": "tick_task",
                "template_id": "tick_flow",
            },
        )
        created = self.client.post(
            "/api/v1/scheduled-tasks",
            json={
                "id": "tick_task",
                "name": "tick task",
                "schedule_type": "one_time",
                "schedule": {"run_at": "2099-01-01T00:00:00Z"},
                "action_type": "publish_trigger_event",
                "action": {},
                "enabled": False,
            },
        )
        self.assertEqual(created.status_code, 201)
        first = self.client.post("/api/v1/scheduled-tasks/tick_task/run").json()
        second = self.client.post("/api/v1/scheduled-tasks/tick_task/run").json()
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(first["result"]["payload"]["sequence"], 1)
        self.assertEqual(second["result"]["payload"]["sequence"], 2)
        self.assertEqual(first["result"]["event_type"], "schedule.tick")

    def test_future_one_time_runs_once_and_disables_itself(self) -> None:
        run_at = datetime.now(timezone.utc) + timedelta(milliseconds=600)
        response = self.client.post(
            "/api/v1/scheduled-tasks",
            json={
                "id": "future_one_time",
                "name": "future one-time",
                "schedule_type": "one_time",
                "schedule": {
                    "run_at": run_at.isoformat(),
                    "misfire_grace_seconds": 5,
                },
                "action_type": "publish_trigger_event",
                "action": {},
                "enabled": True,
            },
        )
        self.assertEqual(response.status_code, 201)
        import time

        deadline = time.monotonic() + 3
        runs: list = []
        while time.monotonic() < deadline:
            runs = self.client.get(
                "/api/v1/scheduled-tasks/future_one_time/runs"
            ).json()
            if runs:
                break
            time.sleep(0.02)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "succeeded")
        task = self.client.get("/api/v1/scheduled-tasks/future_one_time").json()
        self.assertFalse(task["enabled"])
        self.assertIsNone(task["next_run_at"])


class ScheduleExtensionDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_resets_expired_one_time_missed_marker(self) -> None:
        self.fixture = await EngineFixture().start()
        service = OrchestrationApplicationService(self.fixture.engine)
        await service.start()
        try:
            service.create_scheduled_task(
                ScheduledTaskDefinition.model_validate(
                    {
                        "id": "expired_reused",
                        "name": "expired reused",
                        "schedule_type": "one_time",
                        "schedule": {
                            "run_at": "2000-01-01T00:00:00Z",
                            "misfire_grace_seconds": 60,
                        },
                        "action_type": "publish_trigger_event",
                        "action": {},
                        "enabled": True,
                    }
                )
            )
            first = service.get_scheduled_task("expired_reused")
            self.assertFalse(first["enabled"])
            service.update_scheduled_task(
                "expired_reused",
                ScheduledTaskDefinition.model_validate(
                    {
                        "id": first["id"],
                        "version": first["version"],
                        "name": first["name"],
                        "schedule_type": first["schedule_type"],
                        "schedule": {
                            "run_at": "2001-01-01T00:00:00Z",
                            "misfire_grace_seconds": 60,
                        },
                        "action_type": first["action_type"],
                        "action": first["action"],
                        "enabled": True,
                    }
                ),
            )
            updated = service.get_scheduled_task("expired_reused")
            self.assertFalse(updated["enabled"])
            self.assertEqual(updated["last_status"], "failed")
            runs = service.list_scheduled_task_runs(
                "expired_reused", limit=10
            )
            self.assertEqual(len(runs), 2)
            await asyncio.sleep(0.05)
        finally:
            await service.close()
            self.fixture._temp.cleanup()

    async def test_recovered_scheduled_run_writes_internal_outbox(self) -> None:
        self.fixture = await EngineFixture().start()
        service = OrchestrationApplicationService(self.fixture.engine)
        await service.start()
        try:
            service.create_scheduled_task(
                ScheduledTaskDefinition.model_validate(
                    {
                        "id": "crash_recovery_task",
                        "name": "crash recovery task",
                        "schedule_type": "interval",
                        "schedule": {"seconds": 60, "timezone": "UTC"},
                        "action_type": "publish_trigger_event",
                        "action": {},
                        "enabled": False,
                    }
                )
            )
            run = service.store.start_scheduled_task_run(
                "crash_recovery_task",
                scheduled_for="2026-08-20T00:00:00Z",
            )
            recovered = service.store.recover_scheduled_task_runs()
            self.assertEqual(recovered, 1)
            outbox = service.store.list_recoverable_internal_events()
            self.assertTrue(
                any(
                    item["event_type"] == "schedule.run.updated"
                    and item["payload"].get("run_id") == run["id"]
                    and item["payload"].get("status") == "interrupted"
                    for item in outbox
                )
            )
        finally:
            await service.close()
            self.fixture._temp.cleanup()

    async def test_missed_handler_retries_transient_store_failure(self) -> None:
        self.fixture = await EngineFixture().start()
        service = OrchestrationApplicationService(self.fixture.engine)
        await service.start()
        try:
            service.create_scheduled_task(
                ScheduledTaskDefinition.model_validate(
                    {
                        "id": "missed_retry",
                        "name": "missed retry",
                        "schedule_type": "one_time",
                        "schedule": {"run_at": "2099-01-01T00:00:00Z"},
                        "action_type": "publish_trigger_event",
                        "action": {},
                        "enabled": True,
                    }
                )
            )
            original_record = service.store.record_failed_scheduled_task_run
            calls = 0

            def flaky_record(task_id, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("temporary schedule store failure")
                return original_record(task_id, **kwargs)

            service.store.record_failed_scheduled_task_run = flaky_record
            try:
                service.scheduler._spawn_missed_one_time_handler(
                    "missed_retry", "2000-01-01T00:00:00Z"
                )
                for _ in range(200):
                    runs = service.list_scheduled_task_runs(
                        "missed_retry", limit=10
                    )
                    if runs:
                        break
                    await asyncio.sleep(0.01)
            finally:
                service.store.record_failed_scheduled_task_run = original_record
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "failed")
            self.assertFalse(
                service.get_scheduled_task("missed_retry")["enabled"]
            )
            self.assertTrue(service.scheduler.background_errors())
            await asyncio.sleep(0.05)
            self.assertEqual(
                service.scheduler.current_background_failures(), {}
            )
            self.assertEqual(service.health()["status"], "ok")
        finally:
            await service.close()
            self.fixture._temp.cleanup()

    async def test_missed_handler_stops_after_bounded_permanent_errors(self) -> None:
        self.fixture = await EngineFixture().start()
        service = OrchestrationApplicationService(self.fixture.engine)
        await service.start()
        try:
            service.scheduler._spawn_missed_one_time_handler(
                "missing_task", "2000-01-01T00:00:00Z"
            )
            await asyncio.sleep(1.0)
            errors = service.scheduler.background_errors()
            self.assertEqual(len(errors), 1)
            self.assertIn("scheduled task not found", errors[0])
            self.assertTrue(
                all(
                    task.done()
                    for task in service.scheduler._active_tasks
                )
            )
        finally:
            await service.close()
            self.fixture._temp.cleanup()

    async def test_missed_permanent_failure_persists_terminal_error_and_disables(self) -> None:
        self.fixture = await EngineFixture().start()
        service = OrchestrationApplicationService(self.fixture.engine)
        await service.start()
        try:
            service.create_scheduled_task(
                ScheduledTaskDefinition.model_validate(
                    {
                        "id": "missed_permanent",
                        "name": "missed permanent",
                        "schedule_type": "one_time",
                        "schedule": {"run_at": "2099-01-01T00:00:00Z"},
                        "action_type": "publish_trigger_event",
                        "action": {},
                        "enabled": True,
                    }
                )
            )
            original_record = service.store.record_failed_scheduled_task_run
            calls = 0

            def fail_then_record(task_id, **kwargs):
                nonlocal calls
                calls += 1
                if calls <= 3:
                    raise RuntimeError("permanent schedule store failure")
                return original_record(task_id, **kwargs)

            service.store.record_failed_scheduled_task_run = fail_then_record
            try:
                service.scheduler._spawn_missed_one_time_handler(
                    "missed_permanent", "2000-01-01T00:00:00Z"
                )
                await asyncio.sleep(1.0)
            finally:
                service.store.record_failed_scheduled_task_run = original_record
            task = service.get_scheduled_task("missed_permanent")
            self.assertFalse(task["enabled"])
            self.assertIn("missed handler failed permanently", task["scheduler_error"])
            runs = service.list_scheduled_task_runs(
                "missed_permanent", limit=10
            )
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "failed")
            self.assertTrue(
                service.scheduler.current_background_failures()
            )
            self.assertEqual(service.health()["status"], "degraded")
            service.set_scheduled_task_enabled("missed_permanent", True)
            self.assertEqual(
                service.scheduler.current_background_failures(), {}
            )
            self.assertEqual(service.health()["status"], "ok")
            await asyncio.sleep(0.05)
        finally:
            await service.close()
            self.fixture._temp.cleanup()

    async def test_install_failure_is_recorded_once_and_cleared_on_recovery(self) -> None:
        self.fixture = await EngineFixture().start()
        service = OrchestrationApplicationService(self.fixture.engine)
        await service.start()
        try:
            service.create_scheduled_task(
                ScheduledTaskDefinition.model_validate(
                    {
                        "id": "install_fault",
                        "name": "install fault",
                        "schedule_type": "interval",
                        "schedule": {"seconds": 60, "timezone": "UTC"},
                        "action_type": "publish_trigger_event",
                        "action": {},
                        "enabled": False,
                    }
                )
            )
            original_install = service.scheduler._install_job

            def fail_install(record):
                raise RuntimeError("install failure")

            service.scheduler._install_job = fail_install
            try:
                service.set_scheduled_task_enabled(
                    "install_fault", True
                )
            finally:
                service.scheduler._install_job = original_install
            self.assertEqual(
                service.scheduler.background_errors().count("install failure"),
                1,
            )
            self.assertEqual(service.health()["status"], "degraded")
            service.set_scheduled_task_enabled("install_fault", True)
            self.assertEqual(service.health()["status"], "ok")
        finally:
            await service.close()
            self.fixture._temp.cleanup()

    async def test_interval_action_executes_without_binding(self) -> None:
        self.fixture = await EngineFixture().start()
        service = OrchestrationApplicationService(self.fixture.engine)
        await service.start()
        try:
            service.create_scheduled_task(
                ScheduledTaskDefinition.model_validate(
                    {
                        "id": "interval_task",
                        "name": "interval task",
                        "schedule_type": "interval",
                        "schedule": {"seconds": 1, "timezone": "UTC"},
                        "action_type": "publish_trigger_event",
                        "action": {},
                        "enabled": False,
                    }
                )
            )
            run = await service.run_scheduled_task("interval_task")
            self.assertEqual(run["status"], "succeeded")
            self.assertEqual(run["result"]["event_type"], "schedule.tick")
            self.assertEqual(run["result"]["payload"]["schedule_type"], "interval")
        finally:
            await service.close()
            self.fixture._temp.cleanup()
