from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from fastapi import FastAPI

from multi_agent_v2.apps.control_api.main import create_app
from multi_agent_v2.apps.control_api.routes import LivenessResponse
from multi_agent_v2.packages.config import Settings
from multi_agent_v2.packages.observability.health import HealthReport, HealthService


class PassingProbe:
    name = "passing"

    async def check(self) -> None:
        return None


class FailingProbe:
    name = "failing"

    async def check(self) -> None:
        raise RuntimeError("private detail")


def _app(tmp_path: Path, *, healthy: bool) -> FastAPI:
    probe = PassingProbe() if healthy else FailingProbe()
    service = HealthService([probe], timeout_seconds=0.1)
    settings = Settings(artifact_root=tmp_path)
    app = create_app(settings, health_service=service)

    async def _test_write() -> dict[str, str]:
        return {"status": "ok"}

    app.add_api_route("/_test/write", _test_write, methods=["POST"])
    return app


@asynccontextmanager
async def _client(tmp_path: Path, *, healthy: bool) -> AsyncGenerator[httpx2.AsyncClient]:
    app = _app(tmp_path, healthy=healthy)
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


async def test_live_does_not_depend_on_components(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=False) as client:
        response = await client.get("/live")

    assert response.status_code == 200
    assert LivenessResponse.model_validate_json(response.content).status == "ok"


async def test_ready_returns_200_when_components_are_up(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.get("/ready")

    report = HealthReport.model_validate_json(response.content)
    assert response.status_code == 200
    assert report.status == "ready"


async def test_ready_returns_503_without_leaking_failure_message(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=False) as client:
        response = await client.get("/ready")

    report = HealthReport.model_validate_json(response.content)
    assert response.status_code == 503
    assert report.status == "not_ready"
    assert report.components[0].detail == "RuntimeError"
    assert "private detail" not in response.text


async def test_component_health_remains_queryable_while_not_ready(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=False) as client:
        response = await client.get("/health/components")

    report = HealthReport.model_validate_json(response.content)
    assert response.status_code == 200
    assert report.status == "not_ready"


async def test_control_api_rejects_untrusted_host_header(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.get("/live", headers={"host": "untrusted.example"})

    assert response.status_code == 400


async def test_control_api_allows_configured_origin_for_write(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.post(
            "/_test/write",
            headers={"origin": "http://testserver"},
        )

    assert response.status_code == 200


async def test_control_api_allows_loopback_server_call_without_origin(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.post("/_test/write")

    assert response.status_code == 200


async def test_control_api_rejects_cross_origin_write(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.post(
            "/_test/write",
            headers={"origin": "https://evil.example"},
        )

    assert response.status_code == 403
