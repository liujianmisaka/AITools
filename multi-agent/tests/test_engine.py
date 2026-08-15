from __future__ import annotations

import asyncio
import unittest

from multi_agent.domain.models import (
    ApprovalStatus,
    OrchestrationKind,
    TaskSpec,
    WorkflowDefinition,
)
from multi_agent.orchestration.engine import WorkflowEngine
from multi_agent.providers.fake import FakeProvider
from multi_agent.providers.registry import ProviderRegistry
from tests.helpers import EngineFixture, wait_for


class WorkflowEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = await EngineFixture().start()

    async def asyncTearDown(self) -> None:
        await self.fixture.close()

    async def test_dag_runs_parallel_branches_and_renders_outputs(self) -> None:
        workflow = WorkflowDefinition(
            name="parallel",
            max_concurrency=2,
            tasks=[
                TaskSpec(
                    id="analyze",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="analyze",
                    provider_options={"output": "analysis"},
                ),
                TaskSpec(
                    id="left",
                    depends_on=["analyze"],
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="left {{tasks.analyze.output}}",
                    provider_options={"delay": 0.05},
                ),
                TaskSpec(
                    id="right",
                    depends_on=["analyze"],
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="right {{tasks.analyze.output}}",
                    provider_options={"delay": 0.05},
                ),
                TaskSpec(
                    id="merge",
                    depends_on=["left", "right"],
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="merge\n{{dependencies}}",
                ),
            ],
        )

        run_id = await self.fixture.engine.submit(workflow)
        run = await self.fixture.engine.wait(run_id)

        self.assertEqual(run["status"], "succeeded")
        self.assertGreaterEqual(self.fixture.provider.max_active, 2)
        tasks = {
            item["logical_key"]: item
            for item in self.fixture.store.list_work_items(run_id)
        }
        self.assertIn("left analysis", tasks["left"]["final_output"])
        self.assertIn("[left]", tasks["merge"]["final_output"])
        self.assertIn("[right]", tasks["merge"]["final_output"])

    async def test_same_workspace_write_tasks_are_serialized(self) -> None:
        workflow = WorkflowDefinition(
            name="writers",
            max_concurrency=2,
            tasks=[
                TaskSpec(
                    id=task_id,
                    provider="fake",
                    workspace_id="repo",
                    prompt_template=task_id,
                    access="workspace_write",
                    provider_options={"delay": 0.04},
                )
                for task_id in ("one", "two")
            ],
        )

        run_id = await self.fixture.engine.submit(workflow)
        await self.fixture.engine.wait(run_id)

        workspace = str(self.fixture.workspace.resolve())
        self.assertEqual(self.fixture.provider.max_active_by_workspace[workspace], 1)

    async def test_resume_tasks_with_same_session_are_serialized(self) -> None:
        workflow = WorkflowDefinition(
            name="session-lock",
            max_concurrency=2,
            tasks=[
                TaskSpec(
                    id=task_id,
                    provider="fake",
                    workspace_id="repo",
                    prompt_template=task_id,
                    session_mode="resume",
                    provider_session_id="shared-session",
                    provider_options={"delay": 0.04},
                )
                for task_id in ("one", "two")
            ],
        )

        run_id = await self.fixture.engine.submit(workflow)
        await self.fixture.engine.wait(run_id)

        self.assertEqual(self.fixture.provider.max_active, 1)
        self.assertEqual(self.fixture.engine.executor._session_locks, {})

    async def test_read_only_transient_failure_retries(self) -> None:
        workflow = WorkflowDefinition(
            name="retry",
            tasks=[
                TaskSpec(
                    id="retry",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="retry",
                    retry_policy={"max_attempts": 2},
                    provider_options={"failures_before_success": 1, "output": "ok"},
                )
            ],
        )

        run_id = await self.fixture.engine.submit(workflow)
        run = await self.fixture.engine.wait(run_id)
        task = self.fixture.store.get_work_item(run_id, "retry")

        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(task["attempt_count"], 2)
        self.assertEqual(task["final_output"], "ok")

    async def test_approval_pauses_and_resumes_execution(self) -> None:
        workflow = WorkflowDefinition(
            name="approval",
            tasks=[
                TaskSpec(
                    id="write",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="write",
                    access="workspace_write",
                    provider_options={"approval_required": True, "output": "approved"},
                )
            ],
        )

        run_id = await self.fixture.engine.submit(workflow)
        await wait_for(
            lambda: bool(
                self.fixture.store.list_approvals(
                    run_id, status=ApprovalStatus.pending
                )
            )
        )
        approval = self.fixture.store.list_approvals(
            run_id, status=ApprovalStatus.pending
        )[0]
        self.assertEqual(
            self.fixture.store.get_work_item(run_id, "write")["status"],
            "awaiting_approval",
        )

        await self.fixture.engine.resolve_approval(
            approval["id"],
            approved=True,
            decided_by="test",
        )
        run = await self.fixture.engine.wait(run_id)

        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(
            self.fixture.store.get_work_item(run_id, "write")["final_output"],
            "approved",
        )

    async def test_rejected_approval_fails_task(self) -> None:
        workflow = WorkflowDefinition(
            name="approval rejected",
            tasks=[
                TaskSpec(
                    id="write",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="write",
                    access="workspace_write",
                    provider_options={"approval_required": True},
                )
            ],
        )
        run_id = await self.fixture.engine.submit(workflow)
        await wait_for(
            lambda: bool(
                self.fixture.store.list_approvals(
                    run_id, status=ApprovalStatus.pending
                )
            )
        )
        approval = self.fixture.store.list_approvals(
            run_id, status=ApprovalStatus.pending
        )[0]

        await self.fixture.engine.resolve_approval(
            approval["id"],
            approved=False,
            decided_by="test",
            reason="not allowed",
        )
        run = await self.fixture.engine.wait(run_id)

        self.assertEqual(run["status"], "failed")
        self.assertEqual(
            self.fixture.store.get_work_item(run_id, "write")["error_code"],
            "permission_denied",
        )

    async def test_failed_dependency_blocks_only_dependent_branch(self) -> None:
        workflow = WorkflowDefinition(
            name="continue independent",
            tasks=[
                TaskSpec(
                    id="bad",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="bad",
                    provider_options={"failures_before_success": 1},
                ),
                TaskSpec(
                    id="blocked",
                    depends_on=["bad"],
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="blocked",
                ),
                TaskSpec(
                    id="independent",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="independent",
                    provider_options={"output": "ok"},
                ),
            ],
        )

        run_id = await self.fixture.engine.submit(workflow)
        run = await self.fixture.engine.wait(run_id)
        tasks = {
            item["logical_key"]: item
            for item in self.fixture.store.list_work_items(run_id)
        }

        self.assertEqual(run["status"], "failed")
        self.assertEqual(tasks["bad"]["status"], "failed")
        self.assertEqual(tasks["blocked"]["status"], "blocked")
        self.assertEqual(tasks["independent"]["status"], "succeeded")

    async def test_cancel_calls_provider_and_marks_run_cancelled(self) -> None:
        workflow = WorkflowDefinition(
            name="cancel",
            tasks=[
                TaskSpec(
                    id="slow",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="slow",
                    provider_options={"delay": 5},
                )
            ],
        )
        run_id = await self.fixture.engine.submit(workflow)
        await wait_for(
            lambda: self.fixture.store.get_work_item(run_id, "slow")["status"]
            == "running"
        )

        run = await self.fixture.engine.cancel_instance(run_id)

        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(self.fixture.provider.cancel_count, 1)

    async def test_timeout_interrupts_provider_and_releases_run_task(self) -> None:
        workflow = WorkflowDefinition(
            name="timeout",
            tasks=[
                TaskSpec(
                    id="slow",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="slow",
                    timeout_seconds=0.01,
                    provider_options={"delay": 5},
                )
            ],
        )

        run_id = await self.fixture.engine.submit(workflow)
        run = await self.fixture.engine.wait(run_id)
        task = self.fixture.store.get_work_item(run_id, "slow")

        self.assertEqual(run["status"], "failed")
        self.assertEqual(task["error_code"], "timeout")
        self.assertEqual(self.fixture.provider.cancel_count, 1)
        self.assertNotIn(run_id, self.fixture.engine._instance_tasks)

    async def test_restart_resets_shutdown_cancellation_mode(self) -> None:
        await self.fixture.engine.close()
        await self.fixture.engine.start()
        workflow = WorkflowDefinition(
            name="restart then cancel",
            tasks=[
                TaskSpec(
                    id="slow",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="slow",
                    provider_options={"delay": 5},
                )
            ],
        )
        run_id = await self.fixture.engine.submit(workflow)
        await wait_for(
            lambda: self.fixture.store.get_work_item(run_id, "slow")["status"]
            == "running"
        )

        run = await self.fixture.engine.cancel_instance(run_id)

        self.assertEqual(run["status"], "cancelled")

    async def test_orderly_shutdown_rejects_pending_approval(self) -> None:
        workflow = WorkflowDefinition(
            name="approval during shutdown",
            tasks=[
                TaskSpec(
                    id="write",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="write",
                    access="workspace_write",
                    provider_options={"approval_required": True},
                )
            ],
        )
        instance_id = await self.fixture.engine.submit(workflow)
        await wait_for(
            lambda: bool(
                self.fixture.store.list_approvals(
                    instance_id, status=ApprovalStatus.pending
                )
            )
        )
        approval_id = self.fixture.store.list_approvals(
            instance_id, status=ApprovalStatus.pending
        )[0]["id"]

        await self.fixture.engine.close()

        self.assertEqual(
            self.fixture.store.get_instance(instance_id)["status"],
            "interrupted",
        )
        approval = self.fixture.store.get_approval(approval_id)
        self.assertEqual(approval["status"], "rejected")
        self.assertEqual(approval["decided_by"], "system:shutdown")

    async def test_start_resumes_durably_queued_instance(self) -> None:
        workflow = WorkflowDefinition(
            name="resume queued",
            tasks=[
                TaskSpec(
                    id="one",
                    provider="fake",
                    workspace_id="repo",
                    prompt_template="resumed",
                )
            ],
        )
        model = self.fixture.engine.models.get(OrchestrationKind.dag.value)
        instance_id, created = self.fixture.store.create_instance(
            kind=model.kind,
            definition_schema_version=model.definition_schema_version,
            name=workflow.name,
            definition=workflow.model_dump(mode="json"),
            work_items=model.materialize_work_items(workflow),
        )
        self.assertTrue(created)
        await self.fixture.engine.close()

        provider = FakeProvider()
        engine = WorkflowEngine(
            store=self.fixture.store,
            providers=ProviderRegistry([provider]),
            workspaces=self.fixture.engine.workspaces,
        )
        self.fixture.provider = provider
        self.fixture.engine = engine
        recovered = await engine.start()
        instance = await engine.wait(instance_id)

        self.assertEqual(recovered["resumed_instances"], 1)
        self.assertEqual(instance["status"], "succeeded")
        self.assertEqual(
            self.fixture.store.get_work_item(instance_id, "one")["final_output"],
            "resumed",
        )
