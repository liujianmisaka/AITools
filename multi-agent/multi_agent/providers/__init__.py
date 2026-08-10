"""Provider adapters for coding-agent SDKs."""

from multi_agent.providers.base import AgentProvider, ExecutionHandle
from multi_agent.providers.registry import ProviderRegistry

__all__ = ["AgentProvider", "ExecutionHandle", "ProviderRegistry"]
