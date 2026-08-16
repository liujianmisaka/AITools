from __future__ import annotations

from dataclasses import dataclass

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer


@dataclass(frozen=True)
class TelemetryRuntime:
    """Process-owned OpenTelemetry resources without a mandatory external exporter."""

    provider: TracerProvider
    tracer: Tracer

    def shutdown(self) -> None:
        self.provider.shutdown()


def create_telemetry(service_name: str) -> TelemetryRuntime:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    tracer = provider.get_tracer("multi_agent_v2")
    return TelemetryRuntime(provider=provider, tracer=tracer)
