from __future__ import annotations

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from multi_agent_v2.packages.observability import create_telemetry
from multi_agent_v2.packages.observability.health import HealthService


class PassingProbe:
    name = "postgresql"

    async def check(self) -> None:
        return None


async def test_health_probe_emits_service_scoped_span() -> None:
    telemetry = create_telemetry("test-control-api")
    exporter = InMemorySpanExporter()
    telemetry.provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        await HealthService(
            [PassingProbe()],
            timeout_seconds=0.1,
            tracer=telemetry.tracer,
        ).report()
    finally:
        telemetry.shutdown()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = spans[0].attributes
    assert attributes is not None
    assert spans[0].name == "dependency.health"
    assert attributes["dependency.name"] == "postgresql"
    assert attributes["dependency.status"] == "up"
    assert spans[0].resource.attributes["service.name"] == "test-control-api"
