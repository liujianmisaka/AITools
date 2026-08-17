"""Code-owned deterministic event catalog."""

from multi_agent_v2.packages.event_catalog.catalog import EVENT_CATALOG, render_catalog_json
from multi_agent_v2.packages.event_catalog.models import EventDescriptor

__all__ = ["EVENT_CATALOG", "EventDescriptor", "render_catalog_json"]
