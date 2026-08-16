from __future__ import annotations

import asyncio
from collections.abc import Iterable

from multi_agent_v2.packages.agent_runtime.errors import AgentRuntimeError
from multi_agent_v2.packages.agent_runtime.models import (
    AgentErrorInfo,
    AgentModelCatalog,
    AgentRuntimeCapabilities,
    AgentRuntimeDescription,
)
from multi_agent_v2.packages.agent_runtime.protocol import AgentRuntime


class AgentRuntimeRegistry:
    def __init__(self, runtimes: Iterable[AgentRuntime] = ()) -> None:
        self._runtimes: dict[str, AgentRuntime] = {}
        self._closed = False
        for runtime in runtimes:
            self.register(runtime)

    def register(self, runtime: AgentRuntime) -> None:
        if self._closed:
            raise AgentRuntimeError("runtime registry is closed", code="agent.registry_closed")
        if runtime.name in self._runtimes:
            raise AgentRuntimeError(
                f"agent runtime {runtime.name!r} is already registered",
                code="agent.runtime_duplicate",
            )
        self._runtimes[runtime.name] = runtime

    def get(self, name: str) -> AgentRuntime:
        try:
            return self._runtimes[name]
        except KeyError as exc:
            raise AgentRuntimeError(
                f"unknown agent runtime: {name}",
                code="agent.runtime_not_found",
            ) from exc

    async def list_models(self, name: str, *, refresh: bool = False) -> AgentModelCatalog:
        return await self.get(name).list_models(refresh=refresh)

    async def describe(self) -> tuple[AgentRuntimeDescription, ...]:
        names = sorted(self._runtimes)
        descriptions = await asyncio.gather(
            *(self._safe_description(name, self._runtimes[name]) for name in names)
        )
        return tuple(descriptions)

    async def _safe_description(
        self,
        name: str,
        runtime: AgentRuntime,
    ) -> AgentRuntimeDescription:
        try:
            return await runtime.describe()
        except Exception as exc:
            code = getattr(exc, "code", "agent.runtime_unavailable")
            return AgentRuntimeDescription(
                name=name,
                runtime_id=name,
                available=False,
                capabilities=AgentRuntimeCapabilities(),
                error=AgentErrorInfo(code=str(code), message=str(exc)),
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        for name in reversed(tuple(self._runtimes)):
            try:
                await self._runtimes[name].aclose()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("errors while closing agent runtimes", errors)
