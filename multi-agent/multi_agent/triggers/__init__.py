"""Durable event ingestion and trigger-source extension points."""

from multi_agent.triggers.events import (
    EventTypeDefinition,
    EventTypeRegistry,
    default_event_type_registry,
)
from multi_agent.triggers.service import TriggerService
from multi_agent.triggers.sources import (
    EventSourceRegistry,
    FakeEventSource,
    GitCommitEventSource,
    ManualEventSource,
)

__all__ = [
    "EventSourceRegistry",
    "EventTypeDefinition",
    "EventTypeRegistry",
    "FakeEventSource",
    "GitCommitEventSource",
    "ManualEventSource",
    "TriggerService",
    "default_event_type_registry",
]
