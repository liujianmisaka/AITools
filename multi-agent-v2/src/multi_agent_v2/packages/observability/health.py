from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

import structlog
from opentelemetry.trace import Tracer
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)


class ComponentStatus(StrEnum):
    UP = "up"
    DOWN = "down"


class ReadinessStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


class ComponentHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: ComponentStatus
    latency_ms: float
    detail: str | None = None


class HealthReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ReadinessStatus
    components: tuple[ComponentHealth, ...]


class ComponentProbe(Protocol):
    @property
    def name(self) -> str: ...

    async def check(self) -> None: ...


class HealthService:
    def __init__(
        self,
        probes: Sequence[ComponentProbe],
        *,
        timeout_seconds: float,
        tracer: Tracer | None = None,
    ) -> None:
        self._probes = tuple(probes)
        self._timeout_seconds = timeout_seconds
        self._tracer = tracer

    async def report(self) -> HealthReport:
        components = await asyncio.gather(*(self._check(probe) for probe in self._probes))
        status = (
            ReadinessStatus.READY
            if all(component.status is ComponentStatus.UP for component in components)
            else ReadinessStatus.NOT_READY
        )
        return HealthReport(status=status, components=tuple(components))

    async def _check(self, probe: ComponentProbe) -> ComponentHealth:
        if self._tracer is None:
            return await self._check_without_span(probe)
        with self._tracer.start_as_current_span(
            "dependency.health",
            attributes={"dependency.name": probe.name},
        ) as span:
            component = await self._check_without_span(probe)
            span.set_attribute("dependency.status", component.status.value)
            return component

    async def _check_without_span(self, probe: ComponentProbe) -> ComponentHealth:
        started = time.perf_counter()
        try:
            await asyncio.wait_for(probe.check(), timeout=self._timeout_seconds)
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            await logger.awarning(
                "component_health_check_failed",
                component=probe.name,
                error_type=type(exc).__name__,
            )
            return ComponentHealth(
                name=probe.name,
                status=ComponentStatus.DOWN,
                latency_ms=round(latency_ms, 3),
                detail=type(exc).__name__,
            )

        latency_ms = (time.perf_counter() - started) * 1000
        return ComponentHealth(
            name=probe.name,
            status=ComponentStatus.UP,
            latency_ms=round(latency_ms, 3),
        )
