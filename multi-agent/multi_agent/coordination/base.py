from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from multi_agent.coordination.models import AdvisorEnvelope, ContractCheckRequest


class ContractAdvisor(ABC):
    """Advises at a contract boundary and has no workflow execution authority."""

    name: str

    @abstractmethod
    async def evaluate(self, request: ContractCheckRequest) -> AdvisorEnvelope:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"name": self.name}
