from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator

from multi_agent.domain.errors import ProviderCapabilityError
from multi_agent.domain.models import (
    ExecutionRequest,
    ProviderCapabilities,
    ProviderEvent,
    ProviderSessionRef,
)
from multi_agent.providers.catalog import ProviderModelSpec


@dataclass(slots=True)
class ExecutionHandle:
    session: ProviderSessionRef
    native: Any


class AgentProvider(ABC):
    name: str

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    async def models(self) -> tuple[ProviderModelSpec, ...]:
        return ()

    async def metadata(self) -> dict[str, object]:
        return {}

    async def start(self) -> None:
        """Start an optional application-scoped SDK client."""

    async def close(self) -> None:
        """Close application-scoped SDK resources."""

    @abstractmethod
    async def start_execution(
        self,
        request: ExecutionRequest,
        session: ProviderSessionRef | None = None,
    ) -> ExecutionHandle:
        raise NotImplementedError

    @abstractmethod
    def stream(self, handle: ExecutionHandle) -> AsyncIterator[ProviderEvent]:
        raise NotImplementedError

    async def steer(self, handle: ExecutionHandle, prompt: str) -> None:
        raise ProviderCapabilityError(f"provider {self.name!r} does not support steering")

    async def cancel(self, handle: ExecutionHandle) -> None:
        raise ProviderCapabilityError(f"provider {self.name!r} does not support cancellation")

    async def resolve_approval(
        self,
        handle: ExecutionHandle,
        request_id: str,
        approved: bool,
    ) -> None:
        raise ProviderCapabilityError(
            f"provider {self.name!r} does not support approval callbacks"
        )
