from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from multi_agent.domain.models import (
    ScheduledTaskDefinition,
    TriggerBindingDefinition,
    WorkflowDefinition,
)
from multi_agent.orchestration.engine import WorkflowEngine
from multi_agent.orchestration.service import OrchestrationApplicationService
from multi_agent.providers.fake import FakeProvider
from multi_agent.providers.registry import ProviderRegistry
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.workspaces.manager import WorkspaceManager


class GitCommitSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.watcher = self.root / "watcher"
        self.publisher = self.root / "publisher"
        self.database = self.root / "state.sqlite3"

        self._git("init", "--bare", str(self.remote), cwd=self.root)
        self._git("init", "-b", "main", str(self.seed), cwd=self.root)
        self._configure_identity(self.seed)
        (self.seed / "README.md").write_text("initial\n", encoding="utf-8")
        self._git("add", "README.md", cwd=self.seed)
        self._git("commit", "-m", "initial commit", cwd=self.seed)
        self._git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self._git("push", "-u", "origin", "main", cwd=self.seed)
        self._git(
            "--git-dir",
            str(self.remote),
            "symbolic-ref",
            "HEAD",
            "refs/heads/main",
            cwd=self.root,
        )
        self._git("clone", str(self.remote), str(self.watcher), cwd=self.root)
        self._git("clone", str(self.remote), str(self.publisher), cwd=self.root)
        self._configure_identity(self.publisher)

        self.service, self.provider = await self._create_service()

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self._temp.cleanup()

    async def _create_service(
        self,
    ) -> tuple[OrchestrationApplicationService, FakeProvider]:
        provider = FakeProvider()
        engine = WorkflowEngine(
            store=SQLiteStore(self.database),
            providers=ProviderRegistry([provider]),
            workspaces=WorkspaceManager({"watched_repo": self.watcher}),
        )
        service = OrchestrationApplicationService(engine)
        await service.start()
        return service, provider

    def _create_template_binding_and_schedule(self) -> None:
        self.service.create_template(
            WorkflowDefinition.model_validate(
                {
                    "id": "git_update_flow",
                    "name": "Git update flow",
                    "tasks": [
                        {
                            "id": "consume",
                            "provider": "fake",
                            "workspace_id": "watched_repo",
                            "prompt_template": "commit {{input.sha}}",
                        }
                    ],
                }
            )
        )
        self.service.create_trigger_binding(
            TriggerBindingDefinition.model_validate(
                {
                    "id": "watch_main",
                    "name": "Watch origin main",
                    "source_type": "git_commit",
                    "event_type": "git.commit.updated",
                    "event_version": 1,
                    "source_key": "watched_repo:origin:main",
                    "template_id": "git_update_flow",
                    "source_config": {
                        "workspace_id": "watched_repo",
                        "remote": "origin",
                        "branch": "main",
                        "fetch": True,
                    },
                    "input_mapping": {"sha": "payload.after_sha"},
                }
            )
        )
        self.service.create_scheduled_task(
            ScheduledTaskDefinition.model_validate(
                {
                    "id": "poll_main_hourly",
                    "name": "Poll origin main hourly",
                    "schedule": {
                        "expression": "0 * * * *",
                        "timezone": "Asia/Shanghai",
                    },
                    "action": {"binding_id": "watch_main"},
                }
            )
        )

    async def test_git_update_runs_through_durable_schedule_and_survives_restart(
        self,
    ) -> None:
        self._create_template_binding_and_schedule()

        baseline = await self.service.run_scheduled_task("poll_main_hourly")
        self.assertEqual(baseline["status"], "succeeded")
        self.assertEqual(baseline["result"]["published"], [])

        (self.publisher / "change.txt").write_text(
            "scheduled change\n",
            encoding="utf-8",
        )
        self._git("add", "change.txt", cwd=self.publisher)
        self._git("commit", "-m", "scheduled change", cwd=self.publisher)
        self._git("push", "origin", "main", cwd=self.publisher)
        expected_sha = self._git(
            "rev-parse", "HEAD", cwd=self.publisher
        ).stdout.strip()

        detected = await self.service.run_scheduled_task("poll_main_hourly")
        self.assertEqual(detected["status"], "succeeded")
        published = detected["result"]["published"]
        self.assertEqual(len(published), 1)
        event = published[0]
        self.assertEqual(event["event_type"], "git.commit.updated")
        self.assertEqual(event["event_version"], 1)
        self.assertEqual(event["payload"]["after_sha"], expected_sha)
        self.assertEqual(event["payload"]["update_kind"], "forward")
        self.assertEqual(event["payload"]["commit_count"], 1)

        instance_id = event["deliveries"][0]["workflow_instance_id"]
        instance = await self.service.engine.wait(instance_id)
        self.assertEqual(instance["status"], "succeeded")
        self.assertEqual(instance["input"], {"sha": expected_sha})
        self.assertEqual(
            self.service.store.get_work_item(instance_id, "consume")[
                "final_output"
            ],
            f"commit {expected_sha}",
        )

        task_before_restart = self.service.get_scheduled_task(
            "poll_main_hourly"
        )
        self.assertIsNotNone(task_before_restart["next_run_at"])
        self.assertIsNone(task_before_restart["scheduler_error"])
        self.assertEqual(
            len(
                self.service.list_scheduled_task_runs(
                    "poll_main_hourly",
                    limit=10,
                )
            ),
            2,
        )

        await self.service.close()
        self.service, self.provider = await self._create_service()

        restored = self.service.get_scheduled_task("poll_main_hourly")
        self.assertTrue(restored["enabled"])
        self.assertIsNotNone(restored["next_run_at"])
        self.assertEqual(restored["last_status"], "succeeded")
        self.assertEqual(
            len(
                self.service.list_scheduled_task_runs(
                    "poll_main_hourly",
                    limit=10,
                )
            ),
            2,
        )

    @staticmethod
    def _configure_identity(repository: Path) -> None:
        GitCommitSchedulingTests._git(
            "config", "user.name", "Multi Agent Test", cwd=repository
        )
        GitCommitSchedulingTests._git(
            "config",
            "user.email",
            "multi-agent-test@example.invalid",
            cwd=repository,
        )

    @staticmethod
    def _git(
        *arguments: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
