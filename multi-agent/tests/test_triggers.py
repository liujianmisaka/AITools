from __future__ import annotations

import unittest

from multi_agent.domain.errors import TriggerEventProcessingError
from multi_agent.domain.models import (
    TriggerBindingDefinition,
    TriggerEventInput,
    WorkflowDefinition,
)
from multi_agent.orchestration.service import OrchestrationApplicationService
from multi_agent.triggers.events import (
    EventTypeDefinition,
    UnrestrictedPayload,
    default_event_type_registry,
)
from multi_agent.triggers.sources import (
    EventSourceDriver,
    EventSourceRegistry,
    FakeEventSource,
    ManualEventSource,
    SourcePollResult,
)
from tests.helpers import EngineFixture


class _PollOnlySource(EventSourceDriver):
    source_type = "poll_only"
    delivery_mode = "poll"

    async def poll(self, binding, cursor):
        if cursor:
            return SourcePollResult(events=(), cursor=dict(cursor))
        return SourcePollResult(
            events=(
                TriggerEventInput(
                    source_type=self.source_type,
                    source_key=binding["source_key"],
                    event_type=binding["event_type"],
                    dedup_key="poll-only-1",
                    payload={"value": "polled"},
                ),
            ),
            cursor={"seen": True},
        )


class TriggerServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = EngineFixture()
        self.fake_source = FakeEventSource()
        self.poll_only_source = _PollOnlySource()
        event_types = default_event_type_registry()
        event_types.register(
            EventTypeDefinition(
                event_type="test.commit",
                version=1,
                description="Fake commit event used by trigger tests.",
                source_types=("fake",),
                payload_model=UnrestrictedPayload,
            )
        )
        event_types.register(
            EventTypeDefinition(
                event_type="test.tick",
                version=1,
                description="Fake tick event used by trigger tests.",
                source_types=("poll_only",),
                payload_model=UnrestrictedPayload,
            )
        )
        self.service = OrchestrationApplicationService(
            self.fixture.engine,
            event_sources=EventSourceRegistry(
                [
                    ManualEventSource(),
                    self.fake_source,
                    self.poll_only_source,
                ]
            ),
            event_types=event_types,
        )
        await self.service.start()

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.fixture._temp.cleanup()

    def _create_template(self, *, delay: float = 0) -> None:
        self.service.create_template(
            WorkflowDefinition.model_validate(
                {
                    "id": "event_flow",
                    "name": "event flow",
                    "tasks": [
                        {
                            "id": "consume",
                            "provider": "fake",
                            "workspace_id": "repo",
                            "prompt_template": "received {{input.value}}",
                            "provider_options": {"delay": delay},
                        }
                    ],
                }
            )
        )

    async def test_manual_event_is_deduplicated_and_maps_input(self) -> None:
        self._create_template()
        self.service.triggers.create_binding(
            TriggerBindingDefinition(
                id="manual_binding",
                name="manual binding",
                source_type="manual",
                event_type="manual.event",
                template_id="event_flow",
                input_mapping={"value": "payload.amount"},
            )
        )
        event = TriggerEventInput(
            source_type="manual",
            event_type="manual.event",
            dedup_key="event-1",
            payload={"amount": 7},
        )

        first = await self.service.triggers.publish(event)
        instance_id = first["deliveries"][0]["workflow_instance_id"]
        await self.fixture.engine.wait(instance_id)
        duplicate = await self.service.triggers.publish(event)

        self.assertEqual(first["status"], "processed")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(
            duplicate["deliveries"][0]["workflow_instance_id"], instance_id
        )
        self.assertEqual(len(self.fixture.provider.start_calls), 1)
        self.assertEqual(
            self.fixture.store.get_work_item(instance_id, "consume")["final_output"],
            "received 7",
        )

        with self.assertRaises(TriggerEventProcessingError):
            await self.service.triggers.publish(
                event.model_copy(update={"payload": {"amount": 8}})
            )

    async def test_filter_and_skip_if_running_are_durable_delivery_outcomes(self) -> None:
        self._create_template(delay=0.1)
        self.service.triggers.create_binding(
            TriggerBindingDefinition.model_validate(
                {
                    "id": "serialized_binding",
                    "name": "serialized binding",
                    "source_type": "manual",
                    "event_type": "manual.event",
                    "template_id": "event_flow",
                    "event_filter": {"branch": "main"},
                    "input_mapping": {"value": "branch"},
                    "concurrency_policy": "skip_if_running",
                }
            )
        )

        ignored = await self.service.triggers.publish(
            TriggerEventInput(
                source_type="manual",
                event_type="manual.event",
                dedup_key="ignored",
                payload={"branch": "dev"},
            )
        )
        first = await self.service.triggers.publish(
            TriggerEventInput(
                source_type="manual",
                event_type="manual.event",
                dedup_key="main-1",
                payload={"branch": "main"},
            )
        )
        second = await self.service.triggers.publish(
            TriggerEventInput(
                source_type="manual",
                event_type="manual.event",
                dedup_key="main-2",
                payload={"branch": "main"},
            )
        )

        self.assertEqual(ignored["deliveries"], [])
        self.assertEqual(first["deliveries"][0]["status"], "delivered")
        self.assertEqual(second["deliveries"][0]["status"], "skipped")
        await self.fixture.engine.wait(
            first["deliveries"][0]["workflow_instance_id"]
        )

    async def test_fake_poll_source_persists_cursor(self) -> None:
        self._create_template()
        self.service.triggers.create_binding(
            TriggerBindingDefinition(
                id="fake_binding",
                name="fake binding",
                source_type="fake",
                source_key="repo-a",
                event_type="test.commit",
                template_id="event_flow",
                input_mapping={"value": "sha"},
            )
        )
        self.fake_source.emit(
            TriggerEventInput(
                source_type="fake",
                source_key="repo-a",
                event_type="test.commit",
                dedup_key="sha-1",
                payload={"sha": "abc123"},
            )
        )

        first = await self.service.triggers.poll_binding("fake_binding")
        second = await self.service.triggers.poll_binding("fake_binding")

        self.assertEqual(len(first["published"]), 1)
        self.assertEqual(first["cursor"], {"offset": 1})
        self.assertEqual(second["published"], [])
        self.assertEqual(second["cursor"], {"offset": 1})
        instance_id = first["published"][0]["deliveries"][0][
            "workflow_instance_id"
        ]
        await self.fixture.engine.wait(instance_id)
        self.assertEqual(
            self.fixture.store.get_work_item(instance_id, "consume")["final_output"],
            "received abc123",
        )

    async def test_poll_only_source_can_ingest_without_push_permission(self) -> None:
        self._create_template()
        self.service.create_trigger_binding(
            TriggerBindingDefinition(
                id="poll_only_binding",
                name="poll only binding",
                source_type="poll_only",
                source_key="clock-a",
                event_type="test.tick",
                template_id="event_flow",
            )
        )

        result = await self.service.poll_trigger_binding("poll_only_binding")

        self.assertEqual(len(result["published"]), 1)
        self.assertEqual(result["cursor"], {"seen": True})
        instance_id = result["published"][0]["deliveries"][0][
            "workflow_instance_id"
        ]
        await self.fixture.engine.wait(instance_id)
        self.assertEqual(
            self.fixture.store.get_work_item(instance_id, "consume")["final_output"],
            "received polled",
        )
