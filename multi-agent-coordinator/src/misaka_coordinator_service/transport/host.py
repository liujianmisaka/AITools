from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from misaka_coordinator_service.application import (
    CoordinatorActivationRequest,
    CoordinatorAgent,
    CoordinatorAgentConfig,
    CoordinatorAgentError,
    CoordinatorAutonomyPolicy,
    CoordinatorEventBridge,
    CoordinatorMessageResult,
    CoordinatorOrchestrator,
    CoordinatorOrchestratorConfig,
    CoordinatorPolicyError,
    CoordinatorReasoningEffort,
    CoordinatorService,
    CoordinatorServiceApprovalRequiredError,
    CoordinatorServiceError,
    CoordinatorServiceNotFoundError,
    CoordinatorServiceValidationError,
)
from misaka_coordinator_service.domain import CoordinatorSession, PlanNodeStatus, PlanStatus
from misaka_coordinator_service.domain._serialization import ensure_text
from misaka_coordinator_service.execution import (
    V3_DEFAULT_ALLOWED_TOOLS,
    V3_DEFAULT_CAPABILITIES_BY_TOOL,
    DelegationReport,
    DelegationSnapshot,
    JsonValue,
    MessageDelivery,
    ReconciliationStatus,
    V3ExecutionGateway,
    V3SessionGateway,
    V3SessionGatewayConfig,
)
from misaka_coordinator_service.persistence import (
    CoordinatorEventStoreError,
    CoordinatorSessionEvent,
    CoordinatorSessionRecord,
    JsonlCoordinatorEventStore,
    JsonlCoordinatorSessionStore,
    SessionRecordConflictError,
)
from misaka_coordinator_service.tools import (
    JsonlToolAuditSink,
    MAFMCPToolSource,
    MCPToolRegistry,
)

_DEFAULT_OPENCODEX_BASE_URL = "http://127.0.0.1:10100/v1"
_DEFAULT_OPENCODEX_API_KEY_ENV = "OPENAI_API_KEY"
_DEFAULT_OPENCODEX_API_KEY = "opencodex-proxy"


class CoordinatorHostConfigurationError(RuntimeError):
    """Raised when the service host cannot construct its dependencies."""


@dataclass(frozen=True, slots=True)
class CoordinatorHostConfig:
    control_plane_url: str = "http://127.0.0.1:8016"
    state_path: Path = Path(".data/multi-agent-coordinator/sessions.jsonl")
    model: str = "pixel/gpt-5.6-luna"
    reasoning_effort: CoordinatorReasoningEffort = CoordinatorReasoningEffort.MEDIUM
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 8020
    max_decision_steps: int = 16
    wait_timeout_ms: int = 0
    mcp_request_timeout_seconds: int = 30
    max_concurrent_delegations: int = 8
    max_total_delegations: int = 30
    max_delegation_depth: int = 3
    max_plan_revisions: int = 10
    max_retries_per_node: int = 2
    max_runtime_minutes: int = 120
    max_model_activations: int = 50
    allowed_provider_ids: tuple[str, ...] = ()
    allowed_workspace_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "control_plane_url",
            _http_url(self.control_plane_url, "control_plane_url"),
        )
        object.__setattr__(self, "state_path", Path(self.state_path).expanduser().resolve())
        object.__setattr__(self, "model", ensure_text(self.model, "model"))
        try:
            reasoning_effort = CoordinatorReasoningEffort(self.reasoning_effort)
        except ValueError as error:
            raise CoordinatorHostConfigurationError("reasoning_effort is not supported") from error
        object.__setattr__(self, "reasoning_effort", reasoning_effort)
        object.__setattr__(self, "api_key_env", ensure_text(self.api_key_env, "api_key_env"))
        if self.base_url is not None:
            object.__setattr__(self, "base_url", _http_url(self.base_url, "base_url"))
        if not 1 <= self.port <= 65_535:
            raise CoordinatorHostConfigurationError("port must be between 1 and 65535")
        if self.max_decision_steps < 1:
            raise CoordinatorHostConfigurationError("max_decision_steps must be positive")
        if not 0 <= self.wait_timeout_ms <= 300_000:
            raise CoordinatorHostConfigurationError("wait_timeout_ms must be between 0 and 300000")
        if self.mcp_request_timeout_seconds < 1:
            raise CoordinatorHostConfigurationError("mcp_request_timeout_seconds must be positive")
        try:
            policy = self.autonomy_policy
        except CoordinatorPolicyError as error:
            raise CoordinatorHostConfigurationError(str(error)) from error
        object.__setattr__(self, "allowed_provider_ids", policy.allowed_provider_ids)
        object.__setattr__(self, "allowed_workspace_roots", policy.allowed_workspace_roots)

    @property
    def autonomy_policy(self) -> CoordinatorAutonomyPolicy:
        return CoordinatorAutonomyPolicy(
            max_concurrent_delegations=self.max_concurrent_delegations,
            max_total_delegations=self.max_total_delegations,
            max_delegation_depth=self.max_delegation_depth,
            max_plan_revisions=self.max_plan_revisions,
            max_retries_per_node=self.max_retries_per_node,
            max_runtime_minutes=self.max_runtime_minutes,
            max_model_activations=self.max_model_activations,
            allowed_provider_ids=self.allowed_provider_ids,
            allowed_workspace_roots=self.allowed_workspace_roots,
        )


class CoordinatorHostRuntime:
    def __init__(self, config: CoordinatorHostConfig) -> None:
        self.config = config
        self._registry: MCPToolRegistry | None = None
        self._audit_sink: JsonlToolAuditSink | None = None
        self._session_gateway: V3SessionGateway | None = None
        self._event_store: JsonlCoordinatorEventStore | None = None
        self._service: CoordinatorService | None = None
        self._start_lock: asyncio.Lock | None = None

    @property
    def service(self) -> CoordinatorService:
        if self._service is None:
            raise CoordinatorHostConfigurationError("Coordinator host has not started")
        return self._service

    def tool_audits(self) -> tuple[dict[str, object], ...]:
        if self._audit_sink is None:
            return ()
        return tuple(audit.to_dict() for audit in self._audit_sink.records)

    async def start(self) -> None:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self._service is not None:
                return
            source = MAFMCPToolSource.stdio(
                source_id="multi-agent-v3",
                command=sys.executable,
                args=(
                    "-m",
                    "misaka_mcp_gateway",
                    "--control-plane-url",
                    self.config.control_plane_url,
                    "--actor-id",
                    "multi-agent-coordinator",
                    "--actor-kind",
                    "agent",
                    "--scope-id",
                    "coordinator",
                ),
                allowed_tools=V3_DEFAULT_ALLOWED_TOOLS,
                capability_ids_by_tool=V3_DEFAULT_CAPABILITIES_BY_TOOL,
                request_timeout_seconds=self.config.mcp_request_timeout_seconds,
            )
            audit_path = self.config.state_path.with_name(
                f"{self.config.state_path.stem}.tool-audit.jsonl"
            )
            audit_sink = JsonlToolAuditSink(audit_path)
            event_store = JsonlCoordinatorEventStore(
                self.config.state_path.with_name(f"{self.config.state_path.stem}.events.jsonl")
            )
            registry = MCPToolRegistry(
                sources=(source,),
                allowed_tool_names=V3_DEFAULT_ALLOWED_TOOLS,
                audit_sink=audit_sink,
            )
            self._registry = registry
            self._audit_sink = audit_sink
            self._event_store = event_store
            try:
                await registry.refresh()
                if not registry.snapshot.tools:
                    raise CoordinatorHostConfigurationError(
                        "V3 MCP gateway exposed no allowed tools"
                    )
                api_key = os.environ.get(self.config.api_key_env, "").strip()
                if not api_key and _uses_default_local_opencodex(self.config):
                    api_key = _DEFAULT_OPENCODEX_API_KEY
                if not api_key:
                    raise CoordinatorHostConfigurationError(
                        "Coordinator API key environment variable "
                        f"{self.config.api_key_env} is empty"
                    )
                agent = CoordinatorAgent.from_openai(
                    CoordinatorAgentConfig(
                        model=self.config.model,
                        api_key=api_key,
                        base_url=self.config.base_url,
                        reasoning_effort=self.config.reasoning_effort,
                        max_decision_steps=self.config.max_decision_steps,
                    )
                )
                orchestrator = CoordinatorOrchestrator(
                    agent=agent,
                    execution=V3ExecutionGateway(tools=registry),
                    config=CoordinatorOrchestratorConfig(
                        wait_timeout_ms=self.config.wait_timeout_ms,
                        autonomy_policy=self.config.autonomy_policy,
                    ),
                )
                session_gateway = V3SessionGateway(
                    config=V3SessionGatewayConfig(
                        control_plane_url=self.config.control_plane_url,
                        actor_id="multi-agent-coordinator",
                        request_timeout_seconds=self.config.mcp_request_timeout_seconds,
                    )
                )
                self._session_gateway = session_gateway
                service = CoordinatorService(
                    orchestrator=orchestrator,
                    store=JsonlCoordinatorSessionStore(self.config.state_path),
                    event_bridge=CoordinatorEventBridge(
                        source=session_gateway,
                        snapshot_observer=orchestrator,
                        event_observer=orchestrator,
                    ),
                    event_store=event_store,
                )
                await service.start()
                self._service = service
            except Exception:
                if self._session_gateway is not None:
                    await self._session_gateway.aclose()
                    self._session_gateway = None
                await registry.close()
                event_store.close()
                self._registry = None
                self._audit_sink = None
                self._event_store = None
                raise

    async def close(self) -> None:
        if self._service is not None:
            await self._service.aclose()
        if self._session_gateway is not None:
            await self._session_gateway.aclose()
        if self._registry is not None:
            await self._registry.close()
        self._service = None
        self._session_gateway = None
        self._registry = None
        self._audit_sink = None
        self._event_store = None


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivationSubmission(_StrictModel):
    prompt: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    cognitive_session_id: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    activation_id: str | None = None


class CoordinatorSessionSubmission(_StrictModel):
    session_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    cognitive_session_id: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    activation_id: str | None = None


class CoordinatorMessageSubmission(_StrictModel):
    message: str = Field(min_length=1)
    delivery: MessageDelivery = MessageDelivery.APPEND
    expected_activation_id: str | None = None
    model: str | None = None
    effort: str | None = None


class CoordinatorCancelSubmission(_StrictModel):
    reason: str = Field(min_length=1)


class MessageSubmission(_StrictModel):
    node_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    delivery: MessageDelivery = MessageDelivery.APPEND
    expected_activation_id: str | None = None
    model: str | None = None
    effort: str | None = None


class CancelSubmission(_StrictModel):
    node_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    request_id: str | None = None
    idempotency_key: str | None = None
    expected_activation_id: str | None = None


class ReconciliationSubmission(_StrictModel):
    node_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    status: ReconciliationStatus
    reason: str = Field(min_length=1)
    output: object = None
    request_id: str | None = None
    idempotency_key: str | None = None


class CoordinatorAcceptSubmission(_StrictModel):
    expected_session_revision: int = Field(ge=0)


class CoordinatorRetrySubmission(_StrictModel):
    model: str | None = None
    effort: str | None = None


class ApprovalResolutionSubmission(_StrictModel):
    approved: bool
    actor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    expected_session_revision: int = Field(ge=0)


def create_http_application(
    config: CoordinatorHostConfig,
    *,
    runtime: CoordinatorHostRuntime | None = None,
) -> tuple[CoordinatorHostRuntime, FastAPI]:
    host_runtime = runtime or CoordinatorHostRuntime(config)
    mcp_server = FastMCP(
        name="multi-agent-coordinator",
        instructions="Persistent MAF coordinator for Multi-Agent V3 delegations.",
        host=config.host,
        port=config.port,
        stateless_http=True,
        streamable_http_path="/",
    )
    from misaka_coordinator_service.transport.tools import register_tools

    register_tools(mcp_server, host_runtime)
    mcp_application = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await host_runtime.start()
        try:
            async with mcp_application.router.lifespan_context(mcp_application):
                yield
        finally:
            await host_runtime.close()

    app = FastAPI(title="Multi-Agent Coordinator", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(CoordinatorServiceNotFoundError)
    async def handle_not_found(  # pyright: ignore[reportUnusedFunction]
        _request: Request,
        error: CoordinatorServiceNotFoundError,
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "not_found", "message": str(error)})

    @app.exception_handler(SessionRecordConflictError)
    async def handle_conflict(  # pyright: ignore[reportUnusedFunction]
        _request: Request,
        error: SessionRecordConflictError,
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "conflict", "message": str(error)})

    @app.exception_handler(CoordinatorServiceApprovalRequiredError)
    async def handle_approval_required(  # pyright: ignore[reportUnusedFunction]
        _request: Request,
        error: CoordinatorServiceApprovalRequiredError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": "approval_required",
                "message": str(error),
                "approval": error.approval.to_dict(),
            },
        )

    @app.exception_handler(CoordinatorAgentError)
    async def handle_agent_error(  # pyright: ignore[reportUnusedFunction]
        _request: Request,
        error: CoordinatorAgentError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": "coordinator_unavailable", "message": str(error)},
        )

    @app.exception_handler(CoordinatorServiceError)
    async def handle_service_error(  # pyright: ignore[reportUnusedFunction]
        _request: Request,
        error: CoordinatorServiceError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "service_error", "message": str(error)},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ready"}

    @app.get("/sessions")
    async def list_sessions() -> dict[str, list[str]]:  # pyright: ignore[reportUnusedFunction]
        return {"sessions": list(host_runtime.service.list_session_ids())}

    @app.get("/monitors")
    async def list_monitors() -> dict[str, list[dict[str, object]]]:  # pyright: ignore[reportUnusedFunction]
        return {
            "monitors": [status.to_dict() for status in host_runtime.service.monitor_statuses()]
        }

    @app.get("/tool-audits")
    async def list_tool_audits() -> dict[str, list[dict[str, object]]]:  # pyright: ignore[reportUnusedFunction]
        return {"audits": list(host_runtime.tool_audits())}

    @app.get("/coordinator/sessions")
    async def list_coordinator_sessions(  # pyright: ignore[reportUnusedFunction]
        archived: bool = Query(default=False),
    ) -> dict[str, list[dict[str, object]]]:
        return {
            "sessions": [
                _session_summary(record)
                for record in host_runtime.service.list_sessions(archived=archived)
            ]
        }

    @app.post("/coordinator/sessions")
    async def create_coordinator_session(  # pyright: ignore[reportUnusedFunction]
        submission: CoordinatorSessionSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.activate(
            CoordinatorActivationRequest(
                session_id=submission.session_id,
                prompt=submission.prompt,
                cwd=submission.cwd,
                cognitive_session_id=submission.cognitive_session_id,
                acceptance_criteria=tuple(submission.acceptance_criteria),
                constraints=tuple(submission.constraints),
                activation_id=submission.activation_id,
            )
        )
        return result.to_dict()

    @app.get("/coordinator/sessions/{session_id}")
    async def get_coordinator_session(session_id: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return _record_payload(host_runtime.service.get(session_id))

    @app.post("/coordinator/sessions/{session_id}/archive")
    async def archive_coordinator_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
    ) -> dict[str, object]:
        session = await host_runtime.service.archive_session(session_id)
        return {"session": session.to_dict()}

    @app.post("/coordinator/sessions/{session_id}/unarchive")
    async def unarchive_coordinator_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
    ) -> dict[str, object]:
        session = await host_runtime.service.unarchive_session(session_id)
        return {"session": session.to_dict()}

    @app.get("/coordinator/sessions/{session_id}/plan")
    async def get_coordinator_plan(session_id: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        record = host_runtime.service.get(session_id)
        session = record.coordinator_session
        return {
            "session_id": session.session_id,
            "revision": session.revision,
            "plan": None if session.plan is None else session.plan.to_dict(),
            "plan_graph": None if session.plan_graph is None else session.plan_graph.to_dict(),
            "revisions": [item.to_dict() for item in session.plan_revisions],
        }

    @app.get("/coordinator/sessions/{session_id}/node-snapshots")
    async def get_coordinator_node_snapshots(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
    ) -> list[dict[str, object]]:
        snapshots = await host_runtime.service.node_snapshots(session_id=session_id)
        return [
            {"node_id": node_id, "snapshot": _snapshot_payload(snapshot)}
            for node_id, snapshot in snapshots
        ]

    @app.post("/coordinator/sessions/{session_id}/messages", status_code=202)
    async def send_coordinator_message(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        submission: CoordinatorMessageSubmission,
    ) -> dict[str, object]:
        record = host_runtime.service.get(session_id)
        if record.working_directory is None:
            raise CoordinatorServiceValidationError(
                "Coordinator session has no persisted working directory"
            )
        result = await host_runtime.service.activate(
            CoordinatorActivationRequest(
                session_id=session_id,
                prompt=submission.message,
                cwd=record.working_directory,
                cognitive_session_id=record.coordinator_session.cognitive_session_id,
            )
        )
        return result.to_dict()

    @app.post("/coordinator/sessions/{session_id}/nodes/{node_id}/messages", status_code=202)
    async def send_coordinator_node_message(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        node_id: str,
        submission: CoordinatorMessageSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.send_message(
            session_id=session_id,
            node_id=node_id,
            message=submission.message,
            delivery=submission.delivery,
            expected_activation_id=submission.expected_activation_id,
            model=submission.model,
            effort=submission.effort,
        )
        return _message_payload(result)

    @app.post("/coordinator/sessions/{session_id}/nodes/{node_id}/continue", status_code=202)
    async def continue_coordinator_node(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        node_id: str,
        submission: CoordinatorMessageSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.continue_node(
            session_id=session_id,
            node_id=node_id,
            message=submission.message,
            expected_activation_id=submission.expected_activation_id,
            model=submission.model,
            effort=submission.effort,
        )
        return _message_payload(result)

    @app.post("/coordinator/sessions/{session_id}/nodes/{node_id}/reconcile")
    async def reconcile_coordinator_node(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        node_id: str,
        submission: ReconciliationSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.reconcile_node(
            session_id=session_id,
            node_id=node_id,
            expected_revision=submission.expected_revision,
            status=submission.status,
            reason=submission.reason,
            output=cast(JsonValue, submission.output),
            request_id=submission.request_id,
            idempotency_key=submission.idempotency_key,
        )
        return {
            "session": result.session.to_dict(),
            "snapshot": _snapshot_payload(result.snapshot),
        }

    @app.post("/coordinator/sessions/{session_id}/nodes/{node_id}/accept")
    async def accept_coordinator_node(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        node_id: str,
        submission: CoordinatorAcceptSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.accept_result(
            session_id=session_id,
            node_id=node_id,
            expected_session_revision=submission.expected_session_revision,
        )
        return {"session": result.session.to_dict()}

    @app.post("/coordinator/sessions/{session_id}/nodes/{node_id}/retry", status_code=202)
    async def retry_coordinator_node(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        node_id: str,
        submission: CoordinatorRetrySubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.retry_node(
            session_id=session_id,
            node_id=node_id,
            model=submission.model,
            effort=submission.effort,
        )
        return {
            "session": result.session.to_dict(),
            "snapshot": _snapshot_payload(result.snapshot),
        }

    @app.post("/coordinator/sessions/{session_id}/cancel")
    async def cancel_coordinator_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        submission: CoordinatorCancelSubmission,
    ) -> dict[str, object]:
        session = await host_runtime.service.cancel_session(session_id, reason=submission.reason)
        return {"session": session.to_dict()}

    @app.post("/coordinator/sessions/{session_id}/approvals/{approval_id}")
    async def resolve_coordinator_approval(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        approval_id: str,
        submission: ApprovalResolutionSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.resolve_approval(
            session_id=session_id,
            approval_id=approval_id,
            approved=submission.approved,
            actor_id=submission.actor_id,
            reason=submission.reason,
            expected_session_revision=submission.expected_session_revision,
        )
        return result.to_dict()

    @app.get("/coordinator/sessions/{session_id}/events")
    async def list_coordinator_events(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        next_sequence: int = Query(default=1, ge=1),
    ) -> list[dict[str, object]]:
        try:
            events = host_runtime.service.list_events(session_id, next_sequence=next_sequence)
        except CoordinatorEventStoreError as error:
            raise CoordinatorServiceValidationError(str(error)) from error
        return [event.to_dict() for event in events]

    @app.get("/coordinator/sessions/{session_id}/stream")
    async def stream_coordinator_events(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        session_id: str,
        next_sequence: int = Query(default=1, ge=1),
    ) -> StreamingResponse:
        try:
            stream = host_runtime.service.stream_events(
                session_id,
                next_sequence=_stream_start_sequence(request, next_sequence),
            )
        except CoordinatorEventStoreError as error:
            raise CoordinatorServiceValidationError(str(error)) from error

        async def body() -> AsyncIterator[str]:
            iterator = stream.__aiter__()
            pending: asyncio.Future[CoordinatorSessionEvent] | None = None
            last_sequence = _stream_start_sequence(request, next_sequence) - 1
            yield "retry: 3000\n\n"
            try:
                while True:
                    if pending is None:
                        pending = asyncio.ensure_future(iterator.__anext__())
                    done, _ = await asyncio.wait({pending}, timeout=15)
                    if not done:
                        if await request.is_disconnected():
                            return
                        yield ": keep-alive\n\n"
                        continue
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        break
                    pending = None
                    if await request.is_disconnected():
                        return
                    last_sequence = event.sequence
                    yield _sse_event(
                        "coordinator.session.event",
                        event.to_dict(),
                        event_id=str(event.sequence),
                    )
            finally:
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                close_stream = getattr(stream, "aclose", None)
                if close_stream is not None:
                    await close_stream()
            yield _sse_event(
                "coordinator.session.end",
                {"session_id": session_id, "next_sequence": last_sequence + 1},
            )

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return _record_payload(host_runtime.service.get(session_id))

    @app.post("/sessions/{session_id}/activate")
    async def activate(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        submission: ActivationSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.activate(
            CoordinatorActivationRequest(
                session_id=session_id,
                prompt=submission.prompt,
                cwd=submission.cwd,
                cognitive_session_id=submission.cognitive_session_id,
                acceptance_criteria=tuple(submission.acceptance_criteria),
                constraints=tuple(submission.constraints),
                activation_id=submission.activation_id,
            )
        )
        return result.to_dict()

    @app.post("/sessions/{session_id}/messages")
    async def send_message(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        submission: MessageSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.send_message(
            session_id=session_id,
            node_id=submission.node_id,
            message=submission.message,
            delivery=submission.delivery,
            expected_activation_id=submission.expected_activation_id,
            model=submission.model,
            effort=submission.effort,
        )
        return _message_payload(result)

    @app.post("/sessions/{session_id}/continue")
    async def continue_node(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        submission: MessageSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.continue_node(
            session_id=session_id,
            node_id=submission.node_id,
            message=submission.message,
            expected_activation_id=submission.expected_activation_id,
            model=submission.model,
            effort=submission.effort,
        )
        return _message_payload(result)

    @app.post("/sessions/{session_id}/cancel")
    async def cancel_node(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        submission: CancelSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.cancel_node(
            session_id=session_id,
            node_id=submission.node_id,
            reason=submission.reason,
            request_id=submission.request_id,
            idempotency_key=submission.idempotency_key,
            expected_activation_id=submission.expected_activation_id,
        )
        return {
            "session": result.session.to_dict(),
            "snapshot": _snapshot_payload(result.snapshot),
        }

    @app.post("/sessions/{session_id}/reconcile")
    async def reconcile_node(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        submission: ReconciliationSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.reconcile_node(
            session_id=session_id,
            node_id=submission.node_id,
            expected_revision=submission.expected_revision,
            status=submission.status,
            reason=submission.reason,
            output=cast(JsonValue, submission.output),
            request_id=submission.request_id,
            idempotency_key=submission.idempotency_key,
        )
        return {
            "session": result.session.to_dict(),
            "snapshot": _snapshot_payload(result.snapshot),
        }

    @app.post("/sessions/{session_id}/approvals/{approval_id}")
    async def resolve_approval(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        approval_id: str,
        submission: ApprovalResolutionSubmission,
    ) -> dict[str, object]:
        result = await host_runtime.service.resolve_approval(
            session_id=session_id,
            approval_id=approval_id,
            approved=submission.approved,
            actor_id=submission.actor_id,
            reason=submission.reason,
            expected_session_revision=submission.expected_session_revision,
        )
        return result.to_dict()

    app.mount("/mcp", mcp_application)

    return host_runtime, app


def create_mcp_server(config: CoordinatorHostConfig) -> tuple[CoordinatorHostRuntime, FastMCP[Any]]:
    runtime = CoordinatorHostRuntime(config)

    @asynccontextmanager
    async def lifespan(_server: FastMCP[Any]):
        await runtime.start()
        try:
            yield None
        finally:
            await runtime.close()

    server = FastMCP(
        name="multi-agent-coordinator",
        instructions="Persistent MAF coordinator for Multi-Agent V3 delegations.",
        host=config.host,
        port=config.port,
        stateless_http=True,
        lifespan=lifespan,
    )
    from misaka_coordinator_service.transport.tools import register_tools

    register_tools(server, runtime)
    return runtime, server


def _http_url(value: str, field_name: str) -> str:
    normalized = ensure_text(value, field_name).rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CoordinatorHostConfigurationError(f"{field_name} must be an HTTP(S) URL")
    return normalized


def _uses_default_local_opencodex(config: CoordinatorHostConfig) -> bool:
    return (
        config.base_url == _DEFAULT_OPENCODEX_BASE_URL
        and config.api_key_env == _DEFAULT_OPENCODEX_API_KEY_ENV
    )


def _record_payload(record: CoordinatorSessionRecord) -> dict[str, object]:
    return {
        "session": record.coordinator_session.to_dict(),
        "cognitive_session_id": record.agent_session.session_id,
        "working_directory": record.working_directory,
    }


def _session_summary(record: CoordinatorSessionRecord) -> dict[str, object]:
    session = record.coordinator_session
    archive_blocker = CoordinatorService.archive_blocker(record)
    return {
        "session_id": session.session_id,
        "revision": session.revision,
        "goal": None if session.goal is None else session.goal.to_dict(),
        "plan_status": _session_plan_status(session),
        "updated_at": session.updated_at.isoformat(),
        "archived": session.archived_at is not None,
        "archived_at": (None if session.archived_at is None else session.archived_at.isoformat()),
        "archivable": archive_blocker is None,
        "archive_blocker": archive_blocker,
        "working_directory": record.working_directory,
    }


def _session_plan_status(session: CoordinatorSession) -> str | None:
    plan = session.plan
    if plan is None:
        return None
    if plan.status in {
        PlanStatus.COMPLETED,
        PlanStatus.FAILED,
        PlanStatus.CANCELLED,
    }:
        return plan.status.value
    statuses = {node.status for node in plan.nodes}
    if PlanNodeStatus.RECONCILIATION_REQUIRED in statuses:
        return PlanNodeStatus.RECONCILIATION_REQUIRED.value
    if PlanNodeStatus.REVIEW_REQUIRED in statuses:
        return PlanNodeStatus.REVIEW_REQUIRED.value
    if plan.status is PlanStatus.REVIEWING and PlanNodeStatus.REVIEW_REQUIRED not in statuses:
        if PlanNodeStatus.FAILED in statuses:
            return PlanNodeStatus.FAILED.value
        if PlanNodeStatus.CANCELLED in statuses:
            return PlanNodeStatus.CANCELLED.value
    return plan.status.value


def _stream_start_sequence(request: Request, requested: int) -> int:
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is None:
        return requested
    try:
        resumed = int(last_event_id) + 1
    except ValueError:
        return requested
    return max(requested, resumed)


def _sse_event(event_name: str, payload: object, *, event_id: str | None = None) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_name}")
    lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def _message_payload(result: CoordinatorMessageResult) -> dict[str, object]:
    dispatch = result.dispatch
    return {
        "session": result.session.to_dict(),
        "dispatch": {
            "dispatch_id": dispatch.dispatch_id,
            "delegation_id": dispatch.delegation_id,
            "session_id": dispatch.session_id,
            "status": dispatch.status,
            "revision": dispatch.revision,
            "applied_strategy": dispatch.applied_strategy,
            "previous_activation_id": dispatch.previous_activation_id,
            "current_activation_id": dispatch.current_activation_id,
            "error_code": dispatch.error_code,
            "error_message": dispatch.error_message,
        },
    }


def _snapshot_payload(snapshot: DelegationSnapshot) -> dict[str, object]:
    return {
        "delegation_id": snapshot.delegation_id,
        "status": snapshot.status.value,
        "revision": snapshot.revision,
        "session_id": snapshot.session_id,
        "channel_id": snapshot.channel_id,
        "parent_delegation_id": snapshot.parent_delegation_id,
        "depth": snapshot.depth,
        "child_scope": None,
        "current_activation_id": snapshot.current_activation_id,
        "current_invocation_id": snapshot.current_invocation_id,
        "activation_count": snapshot.activation_count,
        "child_delegation_ids": list(snapshot.child_delegation_ids),
        "report": None if snapshot.report is None else _report_payload(snapshot.report),
        "next_action": snapshot.next_action,
    }


def _report_payload(report: DelegationReport) -> dict[str, object]:
    return {
        "status": report.status.value,
        "output": report.output,
        "artifact_ids": list(report.artifact_ids),
        "error_code": report.error_code,
        "error_message": report.error_message,
        "source_invocation_id": report.source_invocation_id,
        "source_activation_id": report.source_activation_id,
        "resolution_reason": None,
        "resolved_by": None,
        "created_at": report.created_at.isoformat(),
    }
