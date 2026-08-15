from __future__ import annotations

import asyncio
import json
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


def _json_output(value: str | None) -> dict:
    if value is None:
        raise AssertionError("Codex task returned no final output")
    candidate = value.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        last_fence = candidate.rfind("```")
        if first_newline < 0 or last_fence <= first_newline:
            raise AssertionError(f"incomplete JSON code fence: {candidate!r}")
        candidate = candidate[first_newline + 1 : last_fence].strip()
    return json.loads(candidate)


@unittest.skipUnless(
    os.getenv("RUN_REAL_CODEX_ADDITION_TESTS") == "1",
    "set RUN_REAL_CODEX_ADDITION_TESTS=1 to allow real Codex requests",
)
class CodexAdditionPipelineRealTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_codex_reads_formulas_then_calculates_dependency(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 1.0
        model = os.environ["REAL_CODEX_MODEL"]
        effort = os.environ["REAL_CODEX_EFFORT"]
        codex_bin = os.getenv("REAL_CODEX_BIN") or shutil.which("codex")
        codex_home = os.environ["REAL_CODEX_HOME"]
        self.assertIsNotNone(codex_bin)

        example_dir = Path(__file__).resolve().parents[2] / "examples" / "addition_pipeline"
        base = WorkflowDefinition.model_validate_json(
            (example_dir / "workflow.json").read_text(encoding="utf-8")
        )
        tasks = [
            task.model_copy(
                update={
                    "provider": "codex",
                    "workspace_id": "addition-real",
                    "timeout_seconds": 240.0,
                    "provider_options": {"model": model, "effort": effort},
                }
            )
            for task in base.tasks
        ]
        workflow = base.model_copy(
            update={"name": "Real Codex two-stage addition pipeline", "tasks": tasks}
        )

        temporary = tempfile.TemporaryDirectory()
        engine = WorkflowEngine(
            store=SQLiteStore(Path(temporary.name) / "state.sqlite3"),
            providers=ProviderRegistry(
                [CodexProvider(codex_bin=codex_bin, codex_home=codex_home)]
            ),
            workspaces=WorkspaceManager({"addition-real": example_dir}),
            max_concurrency=1,
            provider_concurrency={"codex": 1},
        )
        try:
            await engine.start()
            run_id = await engine.submit(workflow)
            async with asyncio.timeout(540):
                run = await engine.wait(run_id)
            formula_task = engine.store.get_task_instance(run_id, "extract_formulas")
            result_task = engine.store.get_task_instance(run_id, "calculate_results")
            events = engine.store.list_events(run_id)
            diagnostics = {
                "run": run,
                "formula_task": formula_task,
                "result_task": result_task,
                "events": [event.model_dump(mode="json") for event in events],
            }

            self.assertEqual(run["status"], "succeeded", diagnostics)
            self.assertEqual(formula_task["status"], "succeeded", diagnostics)
            self.assertEqual(result_task["status"], "succeeded", diagnostics)
            self.assertTrue(formula_task["provider_session_id"], diagnostics)
            self.assertTrue(result_task["provider_session_id"], diagnostics)
            self.assertNotEqual(
                formula_task["provider_session_id"],
                result_task["provider_session_id"],
                diagnostics,
            )
            self.assertEqual(
                formula_task["spec"]["provider_options"],
                {"model": model, "effort": effort},
            )
            self.assertEqual(
                result_task["spec"]["provider_options"],
                {"model": model, "effort": effort},
            )

            formulas = _json_output(formula_task["final_output"])
            results = _json_output(result_task["final_output"])
            self.assertEqual(
                formulas,
                {
                    "formulas": [
                        {"source": "addition_01.txt", "expression": "12 + 30"},
                        {"source": "addition_02.txt", "expression": "7 + 8"},
                        {"source": "addition_03.txt", "expression": "100 + 23"},
                    ]
                },
                diagnostics,
            )
            self.assertEqual(
                results,
                {
                    "results": [
                        {
                            "source": "addition_01.txt",
                            "expression": "12 + 30",
                            "result": 42,
                        },
                        {
                            "source": "addition_02.txt",
                            "expression": "7 + 8",
                            "result": 15,
                        },
                        {
                            "source": "addition_03.txt",
                            "expression": "100 + 23",
                            "result": 123,
                        },
                    ]
                },
                diagnostics,
            )
        finally:
            await engine.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
