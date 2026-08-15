from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from multi_agent.domain.models import WorkflowDefinition
from multi_agent.orchestration.engine import WorkflowEngine
from multi_agent.providers.codex import CodexProvider
from multi_agent.providers.registry import ProviderRegistry
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.workspaces.manager import WorkspaceManager


@unittest.skipUnless(
    os.getenv("RUN_REAL_CODEX_TESTS") == "1",
    "set RUN_REAL_CODEX_TESTS=1 to allow a real Codex request",
)
class CodexRealIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_workflow_passes_preapproved_task_model(self) -> None:
        model = os.environ["REAL_CODEX_MODEL"]
        effort = os.environ["REAL_CODEX_EFFORT"]
        codex_bin = os.getenv("REAL_CODEX_BIN") or shutil.which("codex")
        codex_home = os.environ["REAL_CODEX_HOME"]
        self.assertIsNotNone(codex_bin)
        workspace = Path(os.environ["REAL_CODEX_WORKSPACE"]).resolve()
        self.assertTrue(workspace.is_dir())

        temporary = tempfile.TemporaryDirectory()
        engine = WorkflowEngine(
            store=SQLiteStore(Path(temporary.name) / "state.sqlite3"),
            providers=ProviderRegistry(
                [CodexProvider(codex_bin=codex_bin, codex_home=codex_home)]
            ),
            workspaces=WorkspaceManager({"real-repo": workspace}),
            max_concurrency=1,
            provider_concurrency={"codex": 1},
        )
        try:
            await engine.start()
            workflow = WorkflowDefinition(
                name="Codex real read-only smoke",
                max_concurrency=1,
                tasks=[
                    {
                        "id": "codex-smoke",
                        "provider": "codex",
                        "workspace_id": "real-repo",
                        "access": "read_only",
                        "prompt_template": (
                            "This is a read-only integration smoke test. Do not modify files. "
                            "Reply with exactly CODEX_REAL_SMOKE_OK and nothing else."
                        ),
                        "timeout_seconds": 180,
                        "provider_options": {
                            "model": model,
                            "effort": effort,
                        },
                    }
                ],
            )
            run_id = await engine.submit(workflow)
            async with asyncio.timeout(210):
                run = await engine.wait(run_id)

            task = engine.store.get_task_instance(run_id, "codex-smoke")
            events = engine.store.list_events(run_id)
            diagnostics = {
                "run": run,
                "task": task,
                "events": [event.model_dump(mode="json") for event in events],
            }
            self.assertEqual(run["status"], "succeeded", diagnostics)
            self.assertEqual(task["status"], "succeeded", diagnostics)
            self.assertTrue(task["provider_session_id"])
            self.assertIn("CODEX_REAL_SMOKE_OK", task["final_output"] or "")
            self.assertEqual(
                task["spec"]["provider_options"],
                {"model": model, "effort": effort},
            )
            self.assertTrue(
                any(
                    event.provider == "codex" and event.kind.value == "completed"
                    for event in events
                )
            )
        finally:
            await engine.close()
            temporary.cleanup()
