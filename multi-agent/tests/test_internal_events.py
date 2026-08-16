from __future__ import annotations

import asyncio
import unittest

from multi_agent.domain.models import (
    ApprovalStatus,
    TriggerBindingDefinition,
    WorkflowDefinition,
)
from multi_agent.orchestration.service import OrchestrationApplicationService
from tests.helpers import EngineFixture


class InternalEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = await EngineFixture().start()
        self.service = OrchestrationApplicationService(self.fixture.engine)
        await self.service.start()

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.fixture._temp.cleanup()

    async def test_workflow_status_event_triggers_downstream_template(self) -> None:
        self.service.create_template(
            WorkflowDefinition.model_validate(
                {
                    "id": "downstream",
                    "name": "downstream",
                    "tasks": [
                        {
                            "id": "consume",
                            "provider": "fake",
                            "workspace_id": "repo",
                            "prompt_template": "downstream {{input.workflow_instance_id}}",
                        }
                    ],
                }
            )
        )
        self.service.create_trigger_binding(
            TriggerBindingDefinition.model_validate(
                {
                    "id": "status_binding",
                    "name": "status binding",
                    "source_type": "internal",
                    "event_type": "workflow.instance.status_changed",
                    "template_id": "downstream",
                    "event_filter": {"new_status": "succeeded"},
                }
            )
        )

        upstream_id = await self.fixture.engine.submit(
            WorkflowDefinition.model_validate(
                {
                    "name": "upstream",
                    "tasks": [
                        {
                            "id": "consume",
                            "provider": "fake",
                            "workspace_id": "repo",
                            "prompt_template": "upstream",
                        }
                    ],
                }
            )
        )
        await self.fixture.engine.wait(upstream_id)
        await asyncio.sleep(0.05)

        instances = self.service.store.list_instances(limit=20)["items"]
        downstream = [
            item
            for item in instances
            if item["template_id"] == "downstream"
        ]
        self.assertEqual(len(downstream), 1)
        await self.fixture.engine.wait(downstream[0]["id"])
        self.assertEqual(downstream[0]["status"], "succeeded")

    async def test_cancelled_approval_publishes_rejected_internal_event(self) -> None:
        instance_id = await self.fixture.engine.submit(
            WorkflowDefinition.model_validate(
                {
                    "name": "approval",
                    "tasks": [
                        {
                            "id": "consume",
                            "provider": "fake",
                            "workspace_id": "repo",
                            "prompt_template": "approval",
                            "provider_options": {"approval_required": True},
                        }
                    ],
                }
            )
        )
        for _ in range(200):
            approvals = self.service.list_approvals(
                instance_id, status=ApprovalStatus.pending
            )
            if approvals:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(approvals), 1)

        await self.service.cancel_instance(instance_id)
        await asyncio.sleep(0.05)

        rejected = self.service.list_approvals(
            instance_id, status=ApprovalStatus.rejected
        )
        self.assertEqual(len(rejected), 1)
        events = self.service.list_trigger_events(limit=50)
        rejected_events = [
            event
            for event in events
            if event["event_type"] == "approval.updated"
            and event["payload"].get("status") == "rejected"
            and event["payload"].get("approval_id") == rejected[0]["id"]
        ]
        self.assertEqual(len(rejected_events), 1)

    async def test_failed_internal_dispatch_is_recovered_from_outbox(self) -> None:
        original_ingest = self.service.triggers._ingest

        async def failing_ingest(event):
            if (
                event.source_type == "internal"
                and event.event_type == "workflow.instance.status_changed"
            ):
                raise RuntimeError("temporary inbox failure")
            return await original_ingest(event)

        self.service.triggers._ingest = failing_ingest
        try:
            instance_id = await self.fixture.engine.submit(
                WorkflowDefinition.model_validate(
                    {
                        "name": "outbox recovery",
                        "tasks": [
                            {
                                "id": "consume",
                                "provider": "fake",
                                "workspace_id": "repo",
                                "prompt_template": "ok",
                            }
                        ],
                    }
                )
            )
            await self.fixture.engine.wait(instance_id)
        finally:
            self.service.triggers._ingest = original_ingest

        recoverable = self.service.store.list_recoverable_internal_events()
        self.assertTrue(
            any(
                item["event_type"] == "workflow.instance.status_changed"
                and item["status"] == "failed"
                for item in recoverable
            )
        )
        recovered = await self.service.triggers.recover_internal_outbox()
        self.assertGreaterEqual(recovered, 1)
        self.assertEqual(
            self.service.store.list_recoverable_internal_events(),
            [],
        )
