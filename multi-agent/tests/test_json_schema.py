from __future__ import annotations

import json
import unittest
from pathlib import Path

from multi_agent.domain.json_schema import validate_codex_output_schema


class CodexOutputSchemaTests(unittest.TestCase):
    def test_accepts_addition_pipeline_schemas(self) -> None:
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "addition_pipeline"
            / "workflow.json"
        )
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))

        for task in workflow["tasks"]:
            validate_codex_output_schema(task["output_schema"])

    def test_rejects_missing_additional_properties(self) -> None:
        schema = {
            "type": "object",
            "required": ["result"],
            "properties": {"result": {"type": "integer"}},
        }

        with self.assertRaisesRegex(ValueError, r"\$\.additionalProperties"):
            validate_codex_output_schema(schema)

    def test_rejects_non_strict_nested_object(self) -> None:
        schema = {
            "type": "object",
            "required": ["result"],
            "properties": {
                "result": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "integer"}},
                }
            },
            "additionalProperties": False,
        }

        with self.assertRaisesRegex(
            ValueError,
            r"\$\.properties\.result\.additionalProperties",
        ):
            validate_codex_output_schema(schema)

    def test_rejects_array_without_items(self) -> None:
        schema = {
            "type": "object",
            "required": ["results"],
            "properties": {"results": {"type": "array"}},
            "additionalProperties": False,
        }

        with self.assertRaisesRegex(ValueError, r"results\.items"):
            validate_codex_output_schema(schema)


if __name__ == "__main__":
    unittest.main()
