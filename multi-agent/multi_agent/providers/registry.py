from __future__ import annotations

import asyncio
from collections.abc import Iterable

from multi_agent.domain.errors import ProviderNotFoundError
from multi_agent.providers.base import AgentProvider


class ProviderRegistry:
    def __init__(self, providers: Iterable[AgentProvider] = ()) -> None:
        self._providers: dict[str, AgentProvider] = {}
        self._started: set[str] = set()
        self._start_locks: dict[str, asyncio.Lock] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: AgentProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider {provider.name!r} is already registered")
        self._providers[provider.name] = provider
        self._start_locks[provider.name] = asyncio.Lock()

    def get(self, name: str) -> AgentProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ProviderNotFoundError(f"unknown provider: {name}") from exc

    async def ensure_started(self, name: str) -> AgentProvider:
        provider = self.get(name)
        if name in self._started:
            return provider
        async with self._start_locks[name]:
            if name not in self._started:
                await provider.start()
                self._started.add(name)
        return provider

    def describe(self) -> list[dict[str, object]]:
        descriptions: list[dict[str, object]] = []
        for name, provider in sorted(self._providers.items()):
            description: dict[str, object] = {
                "name": name,
                "started": name in self._started,
                "available": True,
                "capabilities": provider.capabilities().model_dump(mode="json"),
                "models": [],
                "metadata": {},
                "error": None,
            }
            try:
                description["models"] = [
                    model.model_dump(mode="json") for model in provider.models()
                ]
                description["metadata"] = provider.metadata()
            except Exception as exc:
                description["available"] = False
                description["models"] = []
                description["metadata"] = {}
                description["error"] = {
                    "code": getattr(exc, "code", "provider_catalog_unavailable"),
                    "message": str(exc),
                }
            descriptions.append(description)
        return descriptions

    async def close(self) -> None:
        errors: list[BaseException] = []
        for name in reversed(list(self._started)):
            try:
                await self._providers[name].close()
            except BaseException as exc:
                errors.append(exc)
        self._started.clear()
        if errors:
            raise ExceptionGroup("errors while closing providers", errors)
