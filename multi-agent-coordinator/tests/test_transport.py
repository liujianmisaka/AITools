import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from agent_framework import AgentSession
from starlette.routing import Mount

from misaka_coordinator_service.application import (
    CoordinatorMonitorStatus,
    CoordinatorReasoningEffort,
    CoordinatorService,
)
from misaka_coordinator_service.domain import (
    AgentSelection,
    CoordinatorSession,
    ExecutionReference,
    Goal,
    GoalStatus,
    Plan,
    PlanNode,
    TaskIntent,
)
from misaka_coordinator_service.execution import DelegationSnapshot, DelegationStatus
from misaka_coordinator_service.persistence import (
    CoordinatorSessionEvent,
    CoordinatorSessionRecord,
    JsonlCoordinatorEventStore,
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
        "coordinator_accept_result",
        "coordinator_retry",
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


class CoordinatorAPIService:
    def __init__(self) -> None:
        at = datetime(2026, 8, 27, tzinfo=UTC)
        session = CoordinatorSession.create(
            session_id="coordinator-1",
            cognitive_session_id="maf:coordinator-1",
            at=at,
        ).start_goal(
            Goal(
                goal_id="goal:coordinator-1",
                objective="持续推进测试目标",
                acceptance_criteria=(),
                constraints=(),
                status=GoalStatus.ACTIVE,
                created_at=at,
                updated_at=at,
            ),
            at=at,
        )
        self.record = CoordinatorSessionRecord(
            coordinator_session=session,
            agent_session=AgentSession(session_id="maf:coordinator-1"),
            version=1,
            working_directory="D:/workspace",
        )
        self.events = JsonlCoordinatorEventStore()
        self.events.append("coordinator-1", "user.message", {"message": "hello"})

    def require_reconciliation(self) -> None:
        at = datetime(2026, 8, 27, tzinfo=UTC)
        selection = AgentSelection(
            provider_id="codex",
            model_id="codex",
            effort="medium",
            rationale="test",
        )
        node = PlanNode.propose(
            node_id="task-1",
            intent=TaskIntent(task_id="task-1", objective="inspect"),
            at=at,
        ).select(selection, at=at)
        plan = Plan.draft(plan_id="plan-1", goal_id="goal:coordinator-1", at=at)
        plan = plan.add_node(node, at=at).start(at=at)
        node = (
            node.bind_execution(
                ExecutionReference(delegation_id="delegation-1"),
                at=at,
            )
            .await_event(at=at)
            .request_reconciliation(at=at)
        )
        plan = plan.replace_node(node, at=at).review(at=at)
        self.record = CoordinatorSessionRecord(
            coordinator_session=self.record.coordinator_session.attach_plan(plan, at=at),
            agent_session=self.record.agent_session,
            version=self.record.version,
            working_directory=self.record.working_directory,
        )

    def get(self, session_id: str) -> CoordinatorSessionRecord:
        if session_id != self.record.session_id:
            raise AssertionError(session_id)
        return self.record

    def list_session_ids(self, *, archived: bool = False) -> tuple[str, ...]:
        is_archived = self.record.coordinator_session.archived_at is not None
        return (self.record.session_id,) if is_archived == archived else ()

    def list_sessions(self, *, archived: bool = False) -> tuple[CoordinatorSessionRecord, ...]:
        is_archived = self.record.coordinator_session.archived_at is not None
        return (self.record,) if is_archived == archived else ()

    async def archive_session(self, session_id: str) -> CoordinatorSession:
        record = self.get(session_id)
        session = record.coordinator_session
        archived_at = datetime(2026, 8, 28, tzinfo=UTC)
        archived = replace(
            session,
            archived_at=archived_at,
            revision=session.revision + 1,
            updated_at=archived_at,
        )
        self.record = replace(
            record,
            coordinator_session=archived,
            version=record.version + 1,
        )
        return archived

    async def unarchive_session(self, session_id: str) -> CoordinatorSession:
        record = self.get(session_id)
        session = record.coordinator_session
        restored = replace(
            session,
            archived_at=None,
            revision=session.revision + 1,
            updated_at=datetime(2026, 8, 28, 1, tzinfo=UTC),
        )
        self.record = replace(
            record,
            coordinator_session=restored,
            version=record.version + 1,
        )
        return restored

    def list_events(
        self, session_id: str, *, next_sequence: int = 1
    ) -> tuple[CoordinatorSessionEvent, ...]:
        self.get(session_id)
        return self.events.list_events(session_id, next_sequence=next_sequence)

    def stream_events(self, session_id: str, *, next_sequence: int = 1):
        self.get(session_id)
        return self.events.stream_events(session_id, next_sequence=next_sequence)

    async def node_snapshots(self, *, session_id: str):
        record = self.get(session_id)
        plan = record.coordinator_session.plan
        if plan is None:
            return ()
        node = next(candidate for candidate in plan.nodes if candidate.execution is not None)
        execution = node.execution
        assert execution is not None
        return (
            (
                node.node_id,
                DelegationSnapshot(
                    delegation_id=execution.delegation_id,
                    status=DelegationStatus.RECONCILIATION_REQUIRED,
                    revision=2,
                    session_id="session-1",
                    channel_id="channel-1",
                    parent_delegation_id=None,
                    depth=0,
                    current_invocation_id="invocation-1",
                    current_activation_id="activation-1",
                    activation_count=1,
                    child_delegation_ids=(),
                    report=None,
                ),
            ),
        )


class CoordinatorAPIRuntime(FakeRuntime):
    def __init__(self, config: CoordinatorHostConfig, service: CoordinatorAPIService) -> None:
        super().__init__(config)
        self._api_service = service

    @property
    def service(self) -> CoordinatorService:
        return cast(CoordinatorService, self._api_service)


def test_http_application_exposes_coordinator_session_contract(tmp_path: Path) -> None:
    config = CoordinatorHostConfig(state_path=tmp_path / "sessions.jsonl")
    runtime = CoordinatorAPIRuntime(config, CoordinatorAPIService())
    _runtime, application = create_http_application(config, runtime=runtime)

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://coordinator.test",
            ) as client:
                return (
                    await client.get("/coordinator/sessions"),
                    await client.get("/coordinator/sessions/coordinator-1"),
                    await client.get("/coordinator/sessions/coordinator-1/events"),
                    await client.get("/coordinator/sessions/coordinator-1/node-snapshots"),
                )

    sessions, record, events, snapshots = asyncio.run(exercise())
    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["session_id"] == "coordinator-1"
    assert sessions.json()["sessions"][0]["archived"] is False
    assert sessions.json()["sessions"][0]["archived_at"] is None
    assert record.status_code == 200
    assert record.json()["working_directory"] == "D:/workspace"
    assert events.status_code == 200
    assert events.json()[0]["event_type"] == "user.message"
    assert snapshots.status_code == 200
    assert snapshots.json() == []


def test_http_application_archives_and_restores_coordinator_session(tmp_path: Path) -> None:
    config = CoordinatorHostConfig(state_path=tmp_path / "sessions.jsonl")
    runtime = CoordinatorAPIRuntime(config, CoordinatorAPIService())
    _runtime, application = create_http_application(config, runtime=runtime)

    async def exercise() -> tuple[httpx.Response, ...]:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://coordinator.test",
            ) as client:
                archived = await client.post("/coordinator/sessions/coordinator-1/archive")
                active_sessions = await client.get("/coordinator/sessions")
                archived_sessions = await client.get("/coordinator/sessions?archived=true")
                restored = await client.post("/coordinator/sessions/coordinator-1/unarchive")
                restored_sessions = await client.get("/coordinator/sessions")
                return (
                    archived,
                    active_sessions,
                    archived_sessions,
                    restored,
                    restored_sessions,
                )

    archived, active_sessions, archived_sessions, restored, restored_sessions = asyncio.run(
        exercise()
    )

    assert archived.status_code == 200
    assert archived.json()["session"]["archived_at"] is not None
    assert active_sessions.json() == {"sessions": []}
    assert archived_sessions.json()["sessions"][0]["archived"] is True
    assert restored.status_code == 200
    assert restored.json()["session"]["archived_at"] is None
    assert restored_sessions.json()["sessions"][0]["session_id"] == "coordinator-1"


def test_coordinator_session_summary_prioritizes_reconciliation(tmp_path: Path) -> None:
    config = CoordinatorHostConfig(state_path=tmp_path / "sessions.jsonl")
    service = CoordinatorAPIService()
    service.require_reconciliation()
    runtime = CoordinatorAPIRuntime(config, service)
    _runtime, application = create_http_application(config, runtime=runtime)

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://coordinator.test",
            ) as client:
                return (
                    await client.get("/coordinator/sessions"),
                    await client.get("/coordinator/sessions/coordinator-1/node-snapshots"),
                )

    response, snapshots = asyncio.run(exercise())

    assert response.status_code == 200
    assert response.json()["sessions"][0]["plan_status"] == "reconciliation_required"
    assert snapshots.status_code == 200
    assert snapshots.json()[0]["snapshot"]["child_scope"] is None


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
