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
