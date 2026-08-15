from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path

from multi_agent.domain.models import EventKind, ProviderEvent, WorkflowDefinition
from multi_agent.orchestration.engine import WorkflowEngine
from multi_agent.providers.base import ExecutionHandle
from multi_agent.providers.fake import FakeProvider
from multi_agent.providers.registry import ProviderRegistry
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.workspaces.manager import WorkspaceManager

_EXPRESSION = re.compile(r"^\s*(-?\d+)\s*\+\s*(-?\d+)\s*$")
_PAYLOAD_START = "FORMULA_PAYLOAD_BEGIN\n"
_PAYLOAD_END = "\nFORMULA_PAYLOAD_END"


class AdditionFakeProvider(FakeProvider):
    """Executes the example semantics locally without an LLM or external request."""

    def __init__(self) -> None:
        super().__init__(name="addition_fake")

    async def stream(self, handle: ExecutionHandle) -> AsyncIterator[ProviderEvent]:
        request = handle.native.request
        if request.logical_key == "extract_formulas":
            output = self._extract_formulas(request.workspace)
        elif request.logical_key == "calculate_results":
            output = self._calculate_results(request.prompt)
        else:
            raise AssertionError(
                f"unexpected example task: {request.logical_key}"
            )

        yield ProviderEvent(
            kind=EventKind.message_completed,
            summary=output,
            payload={"content": output},
            raw_event_type="addition_fake.message_completed",
        )
        yield ProviderEvent(
            kind=EventKind.completed,
            summary="Addition fake task completed",
            payload={"final_output": output},
            raw_event_type="addition_fake.completed",
        )

    @staticmethod
    def _extract_formulas(workspace: Path) -> str:
        formulas = []
        for path in sorted((workspace / "inputs").glob("*.txt")):
            expression = path.read_text(encoding="utf-8").strip()
            if _EXPRESSION.fullmatch(expression) is None:
                raise AssertionError(f"invalid addition expression in {path.name}")
            formulas.append({"source": path.name, "expression": expression})
        return json.dumps({"formulas": formulas}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _calculate_results(prompt: str) -> str:
        if _PAYLOAD_START not in prompt or _PAYLOAD_END not in prompt:
            raise AssertionError("dependency output markers were not rendered")
        payload_text = prompt.split(_PAYLOAD_START, 1)[1].split(_PAYLOAD_END, 1)[0]
        payload = json.loads(payload_text)
        results = []
        for item in payload["formulas"]:
            match = _EXPRESSION.fullmatch(item["expression"])
            if match is None:
                raise AssertionError("upstream task returned an invalid expression")
            left, right = (int(value) for value in match.groups())
            results.append({**item, "result": left + right})
        return json.dumps({"results": results}, ensure_ascii=False, sort_keys=True)


class AdditionPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_files_then_calculates_from_dependency_output(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 1.0
        example_dir = Path(__file__).resolve().parents[1] / "examples" / "addition_pipeline"
        workflow = WorkflowDefinition.model_validate_json(
            (example_dir / "workflow.json").read_text(encoding="utf-8")
        )
        provider = AdditionFakeProvider()

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = WorkflowEngine(
                store=SQLiteStore(Path(temp_dir) / "state.sqlite3"),
                providers=ProviderRegistry([provider]),
                workspaces=WorkspaceManager({"addition_example": example_dir}),
            )
            await engine.start()
            try:
                run_id = await engine.submit(workflow)
                run = await engine.wait(run_id)
                tasks = {
                    row["logical_key"]: row
                    for row in engine.store.list_work_items(run_id)
                }
            finally:
                await engine.close()

        formulas = json.loads(tasks["extract_formulas"]["final_output"])
        results = json.loads(tasks["calculate_results"]["final_output"])

        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(
            formulas,
            {
                "formulas": [
                    {"source": "addition_01.txt", "expression": "12 + 30"},
                    {"source": "addition_02.txt", "expression": "7 + 8"},
                    {"source": "addition_03.txt", "expression": "100 + 23"},
                ]
            },
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
        )
        self.assertIn(
            tasks["extract_formulas"]["final_output"],
            provider.start_calls[1].prompt,
        )
        self.assertNotIn("inputs/", provider.start_calls[1].prompt)


if __name__ == "__main__":
    unittest.main()
