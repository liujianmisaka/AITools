import asyncio
from pathlib import Path
from typing import cast

import httpx
import pytest
from starlette.routing import Mount

from misaka_coordinator_service.application import (
    CoordinatorMonitorStatus,
    CoordinatorReasoningEffort,
    CoordinatorService,
)
from misaka_coordinator_service.transport import (
    CoordinatorHostConfig,
    CoordinatorHostConfigurationError,
    create_http_application,
    create_mcp_server,
)
from misaka_coordinator_service.transport.host import CoordinatorHostRuntime


def test_host_config_requires_http_control_plane_and_valid_bounds(tmp_path: Path) -> None:
    with pytest.raises(CoordinatorHostConfigurationError, match="HTTP"):
        CoordinatorHostConfig(control_plane_url="not-a-url")
    with pytest.raises(CoordinatorHostConfigurationError, match="between 1"):
        CoordinatorHostConfig(port=0)

    config = CoordinatorHostConfig(state_path=tmp_path / "sessions.jsonl")
    assert config.state_path == (tmp_path / "sessions.jsonl").resolve()
    assert config.reasoning_effort is CoordinatorReasoningEffort.MEDIUM
    assert config.autonomy_policy.max_total_delegations == 30
    with pytest.raises(CoordinatorHostConfigurationError, match="positive"):
        CoordinatorHostConfig(max_total_delegations=0)


def test_host_config_normalizes_local_opencodex_base_url(tmp_path: Path) -> None:
    config = CoordinatorHostConfig(
        state_path=tmp_path / "sessions.jsonl",
        base_url="http://127.0.0.1:10100/v1/",
    )

    assert config.base_url == "http://127.0.0.1:10100/v1"


def test_mcp_server_registers_the_coordinator_tool_surface(tmp_path: Path) -> None:
    _runtime, server = create_mcp_server(
        CoordinatorHostConfig(state_path=tmp_path / "sessions.jsonl")
    )

    assert tuple(tool.name for tool in asyncio.run(server.list_tools())) == (
        "coordinator_activate",
        "coordinator_get_session",
        "coordinator_list_sessions",
        "coordinator_list_monitors",
        "coordinator_list_tool_audits",
        "coordinator_send_message",
        "coordinator_continue",
        "coordinator_cancel",
        "coordinator_reconcile",
        "coordinator_resolve_approval",
    )


class FakeRuntime(CoordinatorHostRuntime):
    def __init__(self, config: CoordinatorHostConfig) -> None:
        super().__init__(config)
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


class MonitorService:
    @staticmethod
    def monitor_statuses() -> tuple[CoordinatorMonitorStatus, ...]:
        return (
            CoordinatorMonitorStatus(
                session_id="coordinator-1",
                node_id="task-1",
                delegation_id="delegation-1",
                running=True,
            ),
        )


class MonitorRuntime(FakeRuntime):
    @property
    def service(self) -> CoordinatorService:
        return cast(CoordinatorService, MonitorService())


def test_http_application_exposes_health_with_lifespan(tmp_path: Path) -> None:
    config = CoordinatorHostConfig(state_path=tmp_path / "sessions.jsonl")
    runtime = FakeRuntime(config)
    _runtime, application = create_http_application(config, runtime=runtime)
    assert any(isinstance(route, Mount) and route.path == "/mcp" for route in application.routes)

    async def exercise() -> httpx.Response:
        async with application.router.lifespan_context(application):
            assert runtime.started is True
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://coordinator.test",
            ) as client:
                return await client.get("/health")

    response = asyncio.run(exercise())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert runtime.closed is True


def test_http_application_validates_json_before_accessing_service(tmp_path: Path) -> None:
    config = CoordinatorHostConfig(state_path=tmp_path / "sessions.jsonl")
    runtime = FakeRuntime(config)
    _runtime, application = create_http_application(config, runtime=runtime)

    async def exercise() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://coordinator.test",
            ) as client:
                return await client.post(
                    "/sessions/session-1/activate",
                    content=b"not-json",
                    headers={"content-type": "application/json"},
                )

    response = asyncio.run(exercise())

    assert response.status_code == 422


def test_http_application_exposes_event_monitor_status(tmp_path: Path) -> None:
    config = CoordinatorHostConfig(state_path=tmp_path / "sessions.jsonl")
    runtime = MonitorRuntime(config)
    _runtime, application = create_http_application(config, runtime=runtime)

    async def exercise() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://coordinator.test",
            ) as client:
                return await client.get("/monitors")

    response = asyncio.run(exercise())

    assert response.status_code == 200
    assert response.json() == {
        "monitors": [
            {
                "session_id": "coordinator-1",
                "node_id": "task-1",
                "delegation_id": "delegation-1",
                "running": True,
                "last_error": None,
            }
        ]
    }
