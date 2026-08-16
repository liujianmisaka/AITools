"""Same-origin Web/BFF boundary for Multi-Agent Platform V2."""

from multi_agent_web_v2.main import create_internal_app, create_public_app
from multi_agent_web_v2.stream_hub import StreamHub

__all__ = ["StreamHub", "create_internal_app", "create_public_app"]
