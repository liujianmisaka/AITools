from __future__ import annotations

import unittest

from pydantic import ValidationError

from multi_agent.domain.models import TaskSpec, WorkflowDefinition


class WorkflowDefinitionTests(unittest.TestCase):
    def test_rejects_cycles_and_unknown_dependencies(self) -> None:
        with self.assertRaisesRegex(ValidationError, "cycle"):
            WorkflowDefinition(
                name="cycle",
                tasks=[
                    TaskSpec(
                        id="a",
                        provider="fake",
                        workspace_id="repo",
                        prompt_template="a",
                        depends_on=["b"],
                    ),
                    TaskSpec(
                        id="b",
                        provider="fake",
                        workspace_id="repo",
                        prompt_template="b",
                        depends_on=["a"],
                    ),
                ],
            )

        with self.assertRaisesRegex(ValidationError, "unknown dependencies"):
            WorkflowDefinition(
                name="missing",
                tasks=[
                    TaskSpec(
                        id="a",
                        provider="fake",
                        workspace_id="repo",
                        prompt_template="a",
                        depends_on=["missing"],
                    )
                ],
            )

    def test_resume_requires_provider_session_id(self) -> None:
        with self.assertRaisesRegex(ValidationError, "provider_session_id"):
            TaskSpec(
                id="resume",
                provider="fake",
                workspace_id="repo",
                prompt_template="continue",
                session_mode="resume",
            )

    def test_new_session_rejects_provider_session_id(self) -> None:
        with self.assertRaises(ValidationError):
            TaskSpec(
                id="task",
                provider="fake",
                workspace_id="repo",
                prompt_template="hello",
                provider_session_id="unexpected-session",
            )

    def test_write_retry_requires_idempotent_flag(self) -> None:
        with self.assertRaisesRegex(ValidationError, "idempotent"):
            TaskSpec(
                id="write",
                provider="fake",
                workspace_id="repo",
                prompt_template="write",
                access="workspace_write",
                retry_policy={"max_attempts": 2},
            )
