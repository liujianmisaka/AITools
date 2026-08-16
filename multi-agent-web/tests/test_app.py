from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from fastapi.testclient import TestClient

from multi_agent_web.core_client import CoreClient
from multi_agent_web.main import create_app


class WebAppTests(unittest.TestCase):
    def _client(self, handler):
        core = CoreClient(
            "http://core.test",
            transport=httpx2.MockTransport(handler),
        )
        return TestClient(create_app(core=core))

    def test_serves_composer_without_importing_core_package(self) -> None:
        async def handler(request):
            return httpx2.Response(200, json={"status": "ok"})

        with self._client(handler) as client:
            page = client.get("/")
            route = client.get("/instances/instance-1")
            trigger_route = client.get("/triggers")
            schedule_route = client.get("/scheduled-tasks")
            legacy = client.get("/legacy")
            script_path = re.search(r'<script[^>]+src="([^"]+\.js)"', page.text)
            self.assertIsNotNone(script_path)
            script = client.get(script_path.group(1))

        self.assertEqual(page.status_code, 200)
        self.assertIn("Multi-Agent Flow", page.text)
        self.assertIn('id="root"', page.text)
        self.assertEqual(route.status_code, 200)
        self.assertIn('id="root"', route.text)
        self.assertEqual(trigger_route.status_code, 200)
        self.assertEqual(schedule_route.status_code, 200)
        self.assertEqual(legacy.status_code, 404)
        self.assertNotIn("legacy-assets", page.text)
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", page.headers["content-security-policy"])
        self.assertEqual(
            page.headers["permissions-policy"],
            "camera=(), microphone=(), geolocation=()",
        )
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertRegex(page.text, r'/assets/index-[A-Za-z0-9_-]+\.css')
        self.assertRegex(page.text, r'/assets/index-[A-Za-z0-9_-]+\.js')
        self.assertEqual(script.status_code, 200)
        self.assertIn("immutable", script.headers["cache-control"])

        frontend = Path(__file__).resolve().parents[1] / "frontend" / "src"
        editor = (frontend / "features/workflows/pages/WorkflowEditorPage.tsx").read_text(encoding="utf-8")
        create_modal = (frontend / "features/workflows/components/TaskCreateModal.tsx").read_text(encoding="utf-8")
        drawer = (frontend / "features/workflows/components/TaskInspectorDrawer.tsx").read_text(encoding="utf-8")
        routes = (frontend / "app/App.tsx").read_text(encoding="utf-8")
        shell = (frontend / "app/layout/AppShell.tsx").read_text(encoding="utf-8")
        triggers_page = (frontend / "features/triggers/pages/TriggersPage.tsx").read_text(encoding="utf-8")
        schedules_page = (frontend / "features/scheduling/pages/ScheduledTasksPage.tsx").read_text(encoding="utf-8")
        contract = (frontend / "shared/lib/workflow.ts").read_text(encoding="utf-8")
        self.assertIn("<TaskCreateModal", editor)
        self.assertIn("<TaskInspectorDrawer", editor)
        self.assertIn("<WorkflowCanvas", editor)
        self.assertIn("任务模型", create_modal)
        self.assertIn("推理等级", create_modal)
        self.assertIn("model_type", create_modal)
        self.assertIn("<Drawer", drawer)
        self.assertIn('path="/instances/:instanceId"', routes)
        self.assertIn('path="/triggers"', routes)
        self.assertIn('path="/scheduled-tasks"', routes)
        self.assertIn("事件触发", shell)
        self.assertIn("定时任务", shell)
        self.assertIn("<TriggerEditorFields", triggers_page)
        self.assertIn("Event Inbox", triggers_page)
        self.assertIn("<ScheduledTaskEditorFields", schedules_page)
        self.assertIn("运行历史", schedules_page)
        self.assertIn("validateCodexOutputSchema", contract)
        self.assertIn("additionalProperties", contract)

        package = Path(__file__).resolve().parents[1] / "multi_agent_web"
        forbidden = re.compile(r"\b(?:from|import)\s+multi_agent(?:\.|\s)")
        for path in package.rglob("*.py"):
            self.assertIsNone(forbidden.search(path.read_text(encoding="utf-8")), path)

    def test_requires_frontend_build_without_restoring_legacy_page(self) -> None:
        async def handler(request):
            return httpx2.Response(200, json={"status": "ok"})

        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_dist = Path(temporary_directory) / "dist"
            with (
                patch("multi_agent_web.main._FRONTEND_DIST_DIR", missing_dist),
                patch(
                    "multi_agent_web.main._FRONTEND_ASSETS_DIR",
                    missing_dist / "assets",
                ),
            ):
                with self._client(handler) as client:
                    page = client.get("/")
                    route = client.get("/instances/instance-1")
                    legacy = client.get("/legacy")
                    health = client.get("/health")

        self.assertEqual(page.status_code, 503)
        self.assertEqual(route.status_code, 503)
        self.assertEqual(legacy.status_code, 404)
        self.assertEqual(health.json(), {"status": "ok"})

    def test_root_dev_scripts_start_three_reload_servers_and_track_pids(self) -> None:
        root = Path(__file__).resolve().parents[2]
        start_script = (root / "start-multi-agent-dev.ps1").read_text(
            encoding="utf-8"
        )
        stop_script = (root / "stop-multi-agent-dev.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("multi_agent.main:app", start_script)
        self.assertIn("multi_agent_web.main:app", start_script)
        self.assertGreaterEqual(start_script.count('"--reload"'), 2)
        self.assertIn("npm.cmd", start_script)
        self.assertIn("FrontendPort = 5173", start_script)
        self.assertIn("Vite HMR", start_script)
        self.assertIn("schema_version = 3", start_script)
        self.assertIn("requiredStateSchemaVersion = 5", start_script)
        self.assertIn("启动脚本不会自动迁移或删除数据", start_script)
        self.assertIn("processes.json", start_script)
        self.assertIn("[switch]$Detached", start_script)
        self.assertIn("监督模式正在运行", start_script)
        self.assertIn("started_at_utc", start_script)
        self.assertIn("taskkill.exe", stop_script)
        self.assertNotIn("Get-CimInstance", start_script)
        self.assertNotIn("Get-CimInstance", stop_script)
        self.assertIn("Stop-TrackedService", stop_script)
        self.assertIn("运行清单已保留", stop_script)

        start_bash = (root / "start-multi-agent-dev.sh").read_text(
            encoding="utf-8"
        )
        stop_bash = (root / "stop-multi-agent-dev.sh").read_text(
            encoding="utf-8"
        )
        self.assertTrue(start_bash.startswith("#!/usr/bin/env bash"))
        self.assertTrue(stop_bash.startswith("#!/usr/bin/env bash"))
        self.assertIn("--detached", start_bash)
        self.assertGreaterEqual(start_bash.count("--reload"), 2)
        self.assertIn("--frontend-port", start_bash)
        self.assertIn("npm.cmd", start_bash)
        self.assertIn("schema_version=3", start_bash)
        self.assertIn('required_state_schema_version="5"', start_bash)
        self.assertIn("launcher never migrates or deletes data", start_bash)
        self.assertIn("processes.git-bash", start_bash)
        self.assertIn("processes.git-bash", stop_bash)
        self.assertIn("MSYS2_ARG_CONV_EXCL='*'", stop_bash)
        self.assertIn("taskkill.exe /PID", stop_bash)
        self.assertIn("core_start_ticks", stop_bash)
        self.assertIn('"$schema_version" == "3"', stop_bash)
        self.assertNotIn("Get-CimInstance", start_bash)
        self.assertNotIn("Get-CimInstance", stop_bash)

    def test_proxies_catalog_validation_and_run_without_rewriting_payload(self) -> None:
        seen: list[tuple[str, str, object | None]] = []
        workflow = {
            "name": "demo",
            "tasks": [
                {
                    "id": "one",
                    "provider": "codex",
                    "workspace_id": "repo",
                    "prompt_template": "Analyze",
                    "provider_options": {"model": "provider/model", "effort": "high"},
                }
            ],
        }

        async def handler(request):
            body = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, body))
            responses = {
                "/health": {"status": "ok"},
                "/api/v1/providers": [
                    {
                        "name": "codex",
                        "capabilities": {},
                        "models": [
                            {
                                "id": "provider/model",
                                "label": "Provider Model",
                                "model_type": "provider",
                                "efforts": ["high"],
                                "default_effort": "high",
                            }
                        ],
                    }
                ],
                "/api/v1/workspaces": {"repo": "D:\\repo"},
                "/api/v1/templates/validate": {
                    "valid": True,
                    "template_id": "generated",
                    "task_count": 1,
                },
                "/api/v1/instances": {"id": "instance-1", "status": "queued"},
                "/api/v1/instances/instance-1": {
                    "id": "instance-1",
                    "status": "succeeded",
                },
                "/api/v1/instances/instance-1/tasks": [
                    {"task_id": "one", "status": "succeeded", "final_output": "done"}
                ],
            }
            return httpx2.Response(200, json=responses[request.url.path])

        with self._client(handler) as client:
            self.assertEqual(client.get("/api/core/health").json(), {"status": "ok"})
            self.assertEqual(client.get("/api/providers").json()[0]["name"], "codex")
            self.assertEqual(client.get("/api/workspaces").json(), {"repo": "D:\\repo"})
            validation = client.post("/api/templates/validate", json=workflow)
            instance = client.post("/api/instances", json=workflow)
            status = client.get("/api/instances/instance-1")
            tasks = client.get("/api/instances/instance-1/tasks")

        self.assertEqual(validation.status_code, 200)
        self.assertTrue(validation.json()["valid"])
        self.assertEqual(instance.status_code, 202)
        self.assertEqual(instance.json()["id"], "instance-1")
        self.assertEqual(status.json()["status"], "succeeded")
        self.assertEqual(tasks.json()[0]["final_output"], "done")
        forwarded = [item for item in seen if item[1] == "/api/v1/instances"][0]
        self.assertEqual(forwarded[2], workflow)

    def test_proxies_template_crud_pagination_and_instantiation(self) -> None:
        seen: list[tuple[str, str, dict[str, str], object | None]] = []
        workflow = {"id": "flow", "version": 1, "name": "saved", "tasks": []}
        record = {
            "id": "flow",
            "version": 1,
            "name": "saved",
            "task_count": 1,
            "definition": workflow,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "archived_at": None,
        }

        async def handler(request):
            body = json.loads(request.content) if request.content else None
            seen.append(
                (
                    request.method,
                    request.url.path,
                    dict(request.url.params),
                    body,
                )
            )
            if request.method == "GET" and request.url.path == "/api/v1/templates":
                return httpx2.Response(200, json={"items": [record], "next_cursor": None})
            if request.url.path == "/api/v1/templates/flow/instances":
                return httpx2.Response(
                    202,
                    json={"id": "instance-1", "status": "queued"},
                )
            return httpx2.Response(200, json=record)

        with self._client(handler) as client:
            created = client.post("/api/templates", json=workflow)
            listed = client.get(
                "/api/templates",
                params={"limit": 20, "cursor": "next", "include_archived": True},
            )
            loaded = client.get("/api/templates/flow")
            updated = client.put("/api/templates/flow", json=workflow)
            archived = client.delete("/api/templates/flow")
            instance = client.post("/api/templates/flow/instances")

        self.assertEqual(created.status_code, 201)
        self.assertEqual(listed.json()["items"][0]["id"], "flow")
        self.assertEqual(loaded.json()["id"], "flow")
        self.assertEqual(updated.json()["version"], 1)
        self.assertEqual(archived.json()["id"], "flow")
        self.assertEqual(instance.status_code, 202)
        list_request = next(
            item
            for item in seen
            if item[0] == "GET" and item[1] == "/api/v1/templates"
        )
        self.assertEqual(list_request[2]["limit"], "20")
        self.assertEqual(list_request[2]["cursor"], "next")
        self.assertEqual(list_request[2]["include_archived"], "true")
        self.assertEqual(
            next(item for item in seen if item[0] == "PUT")[3],
            workflow,
        )

    def test_preserves_core_error_status_and_stable_code(self) -> None:
        async def handler(request):
            return httpx2.Response(
                409,
                json={"detail": "unknown workspace", "code": "workspace_not_allowed"},
            )

        with self._client(handler) as client:
            response = client.post(
                "/api/instances",
                json={"name": "bad", "tasks": []},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"detail": "unknown workspace", "code": "workspace_not_allowed"},
        )

    def test_proxies_instance_list_and_exposes_no_legacy_routes(self) -> None:
        seen: list[tuple[str, str, dict[str, str]]] = []

        async def handler(request):
            seen.append((request.method, request.url.path, dict(request.url.params)))
            return httpx2.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "instance-1",
                            "template_id": "flow",
                            "template_version": 2,
                            "source": "template",
                            "name": "saved",
                            "task_count": 2,
                            "completed_task_count": 1,
                            "status": "running",
                            "error": None,
                            "created_at": "2026-01-01T00:00:00Z",
                            "updated_at": "2026-01-01T00:00:01Z",
                        }
                    ],
                    "next_cursor": "next",
                },
            )

        with self._client(handler) as client:
            listed = client.get(
                "/api/instances",
                params={"limit": 20, "cursor": "cursor", "status": "running"},
            )
            old_workflows = client.get("/api/workflows")
            old_runs = client.get("/api/runs")

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["id"], "instance-1")
        self.assertEqual(
            seen,
            [
                (
                    "GET",
                    "/api/v1/instances",
                    {"limit": "20", "cursor": "cursor", "status": "running"},
                )
            ],
        )
        self.assertEqual(old_workflows.status_code, 404)
        self.assertEqual(old_runs.status_code, 404)

    def test_preserves_structured_validation_errors_and_rejects_invalid_json(self) -> None:
        detail = [{"loc": ["body", "tasks"], "msg": "Field required", "type": "missing"}]

        async def handler(request):
            return httpx2.Response(422, json={"detail": detail})

        with self._client(handler) as client:
            upstream_error = client.post("/api/instances", json={"name": "bad"})
            invalid_json = client.post(
                "/api/instances",
                content="{",
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(upstream_error.status_code, 422)
        self.assertEqual(upstream_error.json()["detail"], detail)
        self.assertEqual(invalid_json.status_code, 400)
        self.assertEqual(
            invalid_json.json()["detail"],
            "request body must contain valid JSON",
        )

    def test_maps_network_failure_to_service_unavailable(self) -> None:
        async def handler(request):
            raise httpx2.ConnectError("offline", request=request)

        with self._client(handler) as client:
            response = client.get("/api/providers")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "core_unavailable")

    def test_proxies_trigger_and_event_contracts_without_core_imports(self) -> None:
        seen: list[tuple[str, str, object | None]] = []

        async def handler(request):
            body = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, body))
            if request.url.path == "/api/v1/event-source-types":
                return httpx2.Response(
                    200,
                    json=[{"source_type": "manual", "delivery_mode": "push"}],
                )
            if request.url.path == "/api/v1/event-types":
                return httpx2.Response(
                    200,
                    json=[{"event_type": "manual.event", "version": 1}],
                )
            if request.url.path == "/api/v1/schedule-types":
                return httpx2.Response(
                    200,
                    json=[{"schedule_type": "cron"}],
                )
            if request.url.path == "/api/v1/scheduled-action-types":
                return httpx2.Response(
                    200,
                    json=[{"action_type": "poll_trigger_binding"}],
                )
            if request.url.path == "/api/v1/orchestration-models":
                return httpx2.Response(
                    200,
                    json=[{"kind": "dag", "definition_schema_version": 1}],
                )
            if request.url.path == "/api/v1/triggers":
                return httpx2.Response(
                    201 if request.method == "POST" else 200,
                    json=body if request.method == "POST" else [],
                )
            if request.url.path == "/api/v1/events":
                return httpx2.Response(
                    202,
                    json={"id": "event-1", "status": "processed"},
                )
            if request.url.path == "/api/v1/scheduled-tasks":
                return httpx2.Response(
                    201 if request.method == "POST" else 200,
                    json=body if request.method == "POST" else [],
                )
            if request.url.path == "/api/v1/scheduled-tasks/task-1/runs":
                return httpx2.Response(
                    200,
                    json=[{"id": "run-1", "status": "succeeded"}],
                )
            return httpx2.Response(200, json={"id": "event-1"})

        trigger = {
            "id": "binding",
            "name": "binding",
            "source_type": "manual",
            "event_type": "commit",
            "template_id": "flow",
        }
        event = {
            "source_type": "manual",
            "event_type": "manual.event",
            "dedup_key": "sha-1",
            "payload": {"sha": "abc"},
        }
        scheduled_task = {
            "id": "task-1",
            "name": "poll main",
            "schedule_type": "cron",
            "schedule": {
                "expression": "*/5 * * * *",
                "timezone": "Asia/Shanghai",
            },
            "action_type": "poll_trigger_binding",
            "action": {"binding_id": "binding"},
        }
        with self._client(handler) as client:
            models = client.get("/api/orchestration-models")
            sources = client.get("/api/event-source-types")
            types = client.get("/api/event-types")
            schedule_types = client.get("/api/schedule-types")
            action_types = client.get("/api/scheduled-action-types")
            created = client.post("/api/triggers", json=trigger)
            published = client.post("/api/events", json=event)
            retried = client.post("/api/events/event-1/retry")
            scheduled = client.post(
                "/api/scheduled-tasks",
                json=scheduled_task,
            )
            runs = client.get("/api/scheduled-tasks/task-1/runs")

        self.assertEqual(models.json()[0]["kind"], "dag")
        self.assertEqual(sources.json()[0]["source_type"], "manual")
        self.assertEqual(types.json()[0]["event_type"], "manual.event")
        self.assertEqual(schedule_types.json()[0]["schedule_type"], "cron")
        self.assertEqual(
            action_types.json()[0]["action_type"],
            "poll_trigger_binding",
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(published.status_code, 202)
        self.assertEqual(retried.json()["id"], "event-1")
        self.assertEqual(scheduled.status_code, 201)
        self.assertEqual(runs.json()[0]["status"], "succeeded")
        self.assertIn(("POST", "/api/v1/triggers", trigger), seen)
        self.assertIn(("POST", "/api/v1/events", event), seen)
        self.assertIn(
            ("POST", "/api/v1/scheduled-tasks", scheduled_task),
            seen,
        )


if __name__ == "__main__":
    unittest.main()
