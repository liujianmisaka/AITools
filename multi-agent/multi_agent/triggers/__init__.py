"""Durable event ingestion and trigger-source extension points."""

from multi_agent.triggers.events import (
    EventTypeDefinition,
    EventTypeRegistry,
    default_event_type_registry,
)
from multi_agent.triggers.service import TriggerService
from multi_agent.triggers.internal import InternalEventPublisher
from multi_agent.triggers.sources import (
    EventSourceRegistry,
    FakeEventSource,
    GitCommitEventSource,
    InternalEventSource,
    ManualEventSource,
    ScheduleEventSource,
    WebhookEventSource,
)

__all__ = [
    "EventSourceRegistry",
    "EventTypeDefinition",
    "EventTypeRegistry",
    "FakeEventSource",
    "GitCommitEventSource",
    "InternalEventPublisher",
    "InternalEventSource",
    "ManualEventSource",
    "ScheduleEventSource",
    "TriggerService",
    "WebhookEventSource",
    "default_event_type_registry",
]
