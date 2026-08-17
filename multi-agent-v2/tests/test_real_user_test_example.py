from __future__ import annotations

from pathlib import Path

from multi_agent_v2.packages.workflow_dsl import (
    CompilationContext,
    ProviderModel,
    compile_workflow,
    parse_json_workflow,
)


def _example() -> str:
    path = Path(__file__).parents[1] / "examples" / "real_user_test" / "workflow.json"
    return path.read_text(encoding="utf-8")


def test_real_user_test_is_a_compilable_explicit_codex_write_task() -> None:
    definition = parse_json_workflow(_example())
    plan = compile_workflow(
        definition,
        CompilationContext(
            catalog_revision="real-user-test-catalog",
            provider_models=(
                ProviderModel(
                    provider="codex",
                    model="sensenova/deepseek-v4-flash",
                    efforts=("high",),
                ),
            ),
            workspace_ids=("aitools",),
            activities=(),
        ),
    )

    assert plan.workflow_id == "real-codex-addition"
    assert len(plan.nodes) == 1
    execution = plan.nodes[0].execution
    assert execution.kind == "agent"
    assert execution.provider == "codex"
    assert execution.model == "sensenova/deepseek-v4-flash"
    assert execution.effort == "high"
    assert execution.workspace_id == "aitools"
    assert execution.access == "workspace_write"
    assert execution.approval_mode == "deny_all"
    assert execution.network_policy == "agent_default"
    assert execution.session_mode == "new"
    assert execution.retry.maximum_attempts == 1
    assert "input/left.txt" in execution.instruction
    assert "input/right.txt" in execution.instruction
    assert "output/result.md" in execution.instruction


def test_real_user_test_inputs_have_the_expected_result() -> None:
    root = Path(__file__).parents[1] / "examples" / "real_user_test" / "input"

    left = int((root / "left.txt").read_text(encoding="utf-8").strip())
    right = int((root / "right.txt").read_text(encoding="utf-8").strip())

    assert f"{left} + {right} = {left + right}" == "37 + 58 = 95"
