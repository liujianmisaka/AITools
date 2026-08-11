"""Durable event ingestion and trigger-source extension points."""

from multi_agent.triggers.service import TriggerService
from multi_agent.triggers.sources import (
    EventSourceRegistry,
    FakeEventSource,
    ManualEventSource,
)

__all__ = [
    "EventSourceRegistry",
    "FakeEventSource",
    "ManualEventSource",
    "TriggerService",
]
