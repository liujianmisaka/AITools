from __future__ import annotations

import time
import unittest

from fastapi.testclient import TestClient

from multi_agent.domain.models import TriggerEventInput
from multi_agent.main import create_app
from multi_agent.providers.fake import FakeProvider
from tests.helpers import EngineFixture


class _BrokenCatalogProvider(FakeProvider):
    async def models(self):
        raise ValueError("catalog is malformed")


class ApiFakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = EngineFixture()
        self.client_context = TestClient(create_app(self.fixture.engine))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.fixture._temp.cleanup()

    def _wait_for_terminal(self, instance_id: str) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/v1/instances/{instance_id}")
            instance = response.json()
            if instance["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return instance
            time.sleep(0.01)
        self.fail("workflow instance did not reach a terminal state")

    def test_health_submit_status_and_sse_resume(self) -> None:
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        providers = self.client.get("/api/v1/providers").json()
        self.assertEqual(providers[0]["models"], [])
        response = self.client.post(
            "/api/v1/instances",
            json={
                "name": "api fake",
                "tasks": [
                    {
                        "id": "one",
                        "provider": "fake",
                        "workspace_id": "repo",
                        "prompt_template": "one",
                        "provider_options": {"output": "done"},
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 202)
        instance_id = response.json()["id"]
        self.assertEqual(response.json()["source"], "ad_hoc")
        self.assertIsNone(response.json()["template_id"])
        self.assertEqual(self._wait_for_terminal(instance_id)["status"], "succeeded")

        stream = self.client.get(f"/api/v1/instances/{instance_id}/events")
        self.assertEqual(stream.status_code, 200)
        event_ids = [
            int(line.removeprefix("id: "))
            for line in stream.text.splitlines()
            if line.startswith("id: ")
        ]
        self.assertGreaterEqual(len(event_ids), 3)

        resumed = self.client.get(
            f"/api/v1/instances/{instance_id}/events",
            headers={"Last-Event-ID": str(event_ids[-2])},
        )
        resumed_ids = [
            int(line.removeprefix("id: "))
            for line in resumed.text.splitlines()
            if line.startswith("id: ")
        ]
        self.assertEqual(resumed_ids, [event_ids[-1]])

    def test_template_crud_conflict_archive_and_instance(self) -> None:
        definition = {
            "id": "saved_flow",
            "version": 1,
            "name": "saved workflow",
            "tasks": [
                {
                    "id": "one",
                    "provider": "fake",
                    "workspace_id": "repo",
                    "prompt_template": "one",
                    "provider_options": {"output": "saved result"},
                }
            ],
        }
        created = self.client.post("/api/v1/templates", json=definition)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["version"], 1)
        duplicate = self.client.post("/api/v1/templates", json=definition)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.json()["code"],
            "workflow_template_version_conflict",
        )

        listed = self.client.get("/api/v1/templates", params={"limit": 20})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["id"], "saved_flow")
        invalid_cursor = self.client.get(
            "/api/v1/templates",
            params={"cursor": "not-a-cursor"},
        )
        self.assertEqual(invalid_cursor.status_code, 400)
        self.assertEqual(
            invalid_cursor.json()["code"],
            "invalid_workflow_template_cursor",
        )
        self.assertEqual(
            self.client.get("/api/v1/templates/saved_flow").json()["definition"]["name"],
            "saved workflow",
        )

        update = {**definition, "name": "updated workflow"}
        updated = self.client.put("/api/v1/templates/saved_flow", json=update)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["version"], 2)
        self.assertEqual(updated.json()["definition"]["version"], 2)

        conflict = self.client.put("/api/v1/templates/saved_flow", json=update)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json()["code"],
            "workflow_template_version_conflict",
        )

        instance = self.client.post("/api/v1/templates/saved_flow/instances")
        self.assertEqual(instance.status_code, 202)
        self.assertEqual(instance.json()["template_id"], "saved_flow")
        self.assertEqual(instance.json()["template_version"], 2)
        self.assertEqual(instance.json()["source"], "template")
        self.assertEqual(
            self._wait_for_terminal(instance.json()["id"])["status"],
            "succeeded",
        )
        instances = self.client.get("/api/v1/instances", params={"limit": 20})
        self.assertEqual(instances.status_code, 200)
        self.assertEqual(instances.json()["items"][0]["id"], instance.json()["id"])
        self.assertEqual(instances.json()["items"][0]["completed_task_count"], 1)

        archived = self.client.delete("/api/v1/templates/saved_flow")
        self.assertEqual(archived.status_code, 200)
        self.assertIsNotNone(archived.json()["archived_at"])
        missing = self.client.get("/api/v1/templates/saved_flow")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "workflow_template_not_found")

        self.assertEqual(self.client.get("/api/v1/workflows").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/runs").status_code, 404)

    def test_rejects_unknown_workspace_before_provider_start(self) -> None:
        response = self.client.post(
            "/api/v1/instances",
            json={
                "name": "bad workspace",
                "tasks": [
                    {
                        "id": "one",
                        "provider": "fake",
                        "workspace_id": "outside",
                        "prompt_template": "one",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "workspace_not_allowed")
        self.assertFalse(self.fixture.provider.started)

    def test_provider_catalog_failure_does_not_break_entire_directory(self) -> None:
        self.fixture.engine.providers.register(_BrokenCatalogProvider("broken"))

        response = self.client.get("/api/v1/providers")

        self.assertEqual(response.status_code, 200)
        providers = {provider["name"]: provider for provider in response.json()}
        self.assertTrue(providers["fake"]["available"])
        self.assertFalse(providers["broken"]["available"])
        self.assertEqual(providers["broken"]["models"], [])
        self.assertEqual(
            providers["broken"]["error"]["code"],
            "provider_catalog_unavailable",
        )

    def test_rejects_invalid_codex_schema_before_task_start(self) -> None:
        codex = FakeProvider("codex")
        self.fixture.engine.providers.register(codex)
        response = self.client.post(
            "/api/v1/templates/validate",
            json={
                "name": "bad schema",
                "tasks": [
                    {
                        "id": "one",
                        "provider": "codex",
                        "workspace_id": "repo",
                        "prompt_template": "one",
                        "output_schema": {
                            "type": "object",
                            "required": ["result"],
                            "properties": {"result": {"type": "integer"}},
                        },
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_output_schema")
        self.assertIn("additionalProperties must be false", response.json()["detail"])
        self.assertFalse(codex.started)

    def test_pi_interface_is_reserved_but_not_wired(self) -> None:
        coordinator = self.client.get("/api/v1/coordinator")
        self.assertEqual(coordinator.status_code, 200)
        payload = coordinator.json()
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["authority"], "reserved_contract_advice_only")
        self.assertFalse(payload["can_create_templates"])
        self.assertFalse(payload["can_submit_instances"])
        self.assertEqual(payload["execution_entrypoint"], "/api/v1/instances")
        self.assertEqual(payload["invocation"], "not_wired")

        self.assertEqual(self.client.post("/api/v1/plans", json={}).status_code, 404)
        self.assertEqual(
            self.client.post("/api/v1/plans/submit", json={}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/api/v1/coordinator/evaluate", json={}).status_code,
            404,
        )

    def test_event_ingress_creates_a_triggered_template_instance(self) -> None:
        template = self.client.post(
            "/api/v1/templates",
            json={
                "id": "event_api_flow",
                "name": "event API flow",
                "tasks": [
                    {
                        "id": "consume",
                        "provider": "fake",
                        "workspace_id": "repo",
                        "prompt_template": "commit {{input.sha}}",
                    }
                ],
            },
        )
        self.assertEqual(template.status_code, 201)
        self.assertEqual(template.json()["kind"], "dag")
        self.assertEqual(
            self.client.get("/api/v1/orchestration-models").json(),
            [{"kind": "dag", "definition_schema_version": 1}],
        )
        source_types = {
            item["source_type"]
            for item in self.client.get("/api/v1/event-source-types").json()
        }
        self.assertEqual(
            source_types,
            {"git_commit", "internal", "manual", "schedule", "webhook"},
        )
        event_types = {
            (item["event_type"], item["version"])
            for item in self.client.get("/api/v1/event-types").json()
        }
        self.assertIn(("git.commit.updated", 1), event_types)
        self.assertIn(("manual.event", 1), event_types)
        self.assertEqual(
            self.client.get("/api/v1/schedule-types").json()[0][
                "schedule_type"
            ],
            "cron",
        )
        self.assertEqual(
            self.client.get("/api/v1/scheduled-action-types").json()[0][
                "action_type"
            ],
            "poll_trigger_binding",
        )

        trigger = self.client.post(
            "/api/v1/triggers",
            json={
                "id": "event_api_binding",
                "name": "event API binding",
                "source_type": "manual",
                "event_type": "manual.event",
                "template_id": "event_api_flow",
                "input_mapping": {"sha": "payload.after"},
            },
        )
        self.assertEqual(trigger.status_code, 201)

        event = self.client.post(
            "/api/v1/events",
            json={
                "source_type": "manual",
                "event_type": "manual.event",
                "dedup_key": "repo:abc123",
                "payload": {"after": "abc123"},
            },
        )
        self.assertEqual(event.status_code, 202)
        payload = event.json()
        self.assertEqual(payload["status"], "processed")
        self.assertEqual(payload["deliveries"][0]["status"], "delivered")
        instance_id = payload["deliveries"][0]["workflow_instance_id"]
        instance = self._wait_for_terminal(instance_id)
        self.assertEqual(instance["cause_type"], "trigger")
        self.assertEqual(instance["input"], {"sha": "abc123"})

        duplicate = self.client.post(
            "/api/v1/events",
            json={
                "source_type": "manual",
                "event_type": "manual.event",
                "dedup_key": "repo:abc123",
                "payload": {"after": "abc123"},
            },
        )
        self.assertTrue(duplicate.json()["deduplicated"])
        self.assertEqual(len(self.fixture.provider.start_calls), 1)

        archived = self.client.delete("/api/v1/templates/event_api_flow")
        self.assertEqual(archived.status_code, 409)
        self.assertEqual(archived.json()["code"], "trigger_binding_conflict")

    def test_dead_letter_outbox_has_list_and_retry_api(self) -> None:
        outbox = self.fixture.store.enqueue_internal_event(
            TriggerEventInput(
                source_type="internal",
                event_type="schedule.run.updated",
                event_version=1,
                source_key="dead-api-task",
                dedup_key="dead-api-row",
                payload={
                    "scheduled_task_id": "dead-api-task",
                    "run_id": "dead-api-run",
                    "status": "failed",
                    "scheduled_for": None,
                    "error": "test",
                },
            )
        )
        for _ in range(5):
            self.fixture.store.mark_internal_event_failed(
                outbox["id"], "permanent failure"
            )

        listed = self.client.get("/api/v1/events/outbox/dead-letter")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)
        self.assertEqual(listed.json()[0]["id"], outbox["id"])

        retried = self.client.post(
            "/api/v1/events/outbox/dead-letter/retry"
        )
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["reset"], 1)
        self.assertEqual(retried.json()["dead_letter_count"], 0)

    def test_scheduled_task_api_persists_cron_definition_and_runs(self) -> None:
        self.client.post(
            "/api/v1/templates",
            json={
                "id": "scheduled_api_flow",
                "name": "scheduled API flow",
                "tasks": [
                    {
                        "id": "consume",
                        "provider": "fake",
                        "workspace_id": "repo",
                        "prompt_template": "received {{input.value}}",
                    }
                ],
            },
        )
        self.client.post(
            "/api/v1/triggers",
            json={
                "id": "scheduled_fake_binding",
                "name": "scheduled fake binding",
                "source_type": "git_commit",
                "event_type": "git.commit.updated",
                "event_version": 1,
                "source_key": "repo:origin:main",
                "template_id": "scheduled_api_flow",
                "source_config": {
                    "workspace_id": "repo",
                    "remote": "origin",
                    "branch": "main",
                    "fetch": False,
                },
            },
        )
        response = self.client.post(
            "/api/v1/scheduled-tasks",
            json={
                "id": "scheduled_api_task",
                "name": "scheduled API task",
                "schedule_type": "cron",
                "schedule": {
                    "expression": "0 * * * *",
                    "timezone": "Asia/Shanghai",
                },
                "action_type": "poll_trigger_binding",
                "action": {"binding_id": "scheduled_fake_binding"},
                "enabled": False,
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["version"], 1)
        self.assertFalse(response.json()["enabled"])
        self.assertIsNone(response.json()["next_run_at"])
        self.assertEqual(response.json()["schedule"]["timezone"], "Asia/Shanghai")
        self.assertEqual(
            response.json()["schedule"]["misfire_grace_seconds"],
            60,
        )
        self.assertTrue(response.json()["schedule"]["coalesce"])
        listed = self.client.get("/api/v1/scheduled-tasks")
        self.assertEqual(listed.json()[0]["id"], "scheduled_api_task")
        enabled = self.client.post(
            "/api/v1/scheduled-tasks/scheduled_api_task/enable"
        )
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(enabled.json()["version"], 2)
        self.assertTrue(enabled.json()["enabled"])
        self.assertIsNotNone(enabled.json()["next_run_at"])
        trigger_disabled = self.client.post(
            "/api/v1/triggers/scheduled_fake_binding/disable"
        )
        self.assertEqual(trigger_disabled.status_code, 200)
        paused = self.client.get(
            "/api/v1/scheduled-tasks/scheduled_api_task"
        ).json()
        self.assertIsNone(paused["next_run_at"])
        self.assertIn("is disabled", paused["scheduler_error"])
        trigger_enabled = self.client.post(
            "/api/v1/triggers/scheduled_fake_binding/enable"
        )
        self.assertEqual(trigger_enabled.status_code, 200)
        restored = self.client.get(
            "/api/v1/scheduled-tasks/scheduled_api_task"
        ).json()
        self.assertIsNotNone(restored["next_run_at"])
        self.assertIsNone(restored["scheduler_error"])
        disabled = self.client.post(
            "/api/v1/scheduled-tasks/scheduled_api_task/disable"
        )
        self.assertEqual(disabled.json()["version"], 3)
        self.assertIsNone(disabled.json()["next_run_at"])
        self.assertEqual(
            self.client.get(
                "/api/v1/scheduled-tasks/scheduled_api_task/runs"
            ).json(),
            [],
        )
