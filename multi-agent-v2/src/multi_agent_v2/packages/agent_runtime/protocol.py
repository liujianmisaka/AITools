from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from multi_agent_v2.packages.agent_runtime.models import (
    AgentEvent,
    AgentExecutionRequest,
    AgentModelCatalog,
    AgentReconcileRequest,
    AgentRuntimeDescription,
    AgentTurnHandle,
    CancelResult,
    PreparedAgentSession,
    ReconcileResult,
)


@runtime_checkable
class AgentRuntime(Protocol):
    name: str

    async def describe(self) -> AgentRuntimeDescription: ...

    async def list_models(self, *, refresh: bool = False) -> AgentModelCatalog: ...

    async def validate_request(self, request: AgentExecutionRequest) -> None: ...

    async def prepare_session(
        self,
        request: AgentExecutionRequest,
    ) -> PreparedAgentSession: ...

    async def start_turn(
        self,
        request: AgentExecutionRequest,
        session: PreparedAgentSession,
    ) -> AgentTurnHandle: ...

    def stream(self, handle: AgentTurnHandle) -> AsyncIterator[AgentEvent]: ...

    async def steer(self, handle: AgentTurnHandle, message: str) -> None: ...

    async def cancel(self, handle: AgentTurnHandle) -> CancelResult: ...

    async def reconcile(self, request: AgentReconcileRequest) -> ReconcileResult: ...

    async def attach(self, request: AgentReconcileRequest) -> AgentTurnHandle: ...

    async def aclose(self) -> None: ...
