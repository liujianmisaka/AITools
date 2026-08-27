from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from misaka_coordinator_service.application import (
    CoordinatorActivationRequest,
    CoordinatorAgent,
    CoordinatorAgentConfig,
    CoordinatorAgentError,
    CoordinatorMessageResult,
    CoordinatorOrchestrator,
    CoordinatorOrchestratorConfig,
    CoordinatorReasoningEffort,
    CoordinatorService,
    CoordinatorServiceError,
    CoordinatorServiceNotFoundError,
)
from misaka_coordinator_service.domain._serialization import ensure_text
from misaka_coordinator_service.execution import (
    V3_DEFAULT_ALLOWED_TOOLS,
    V3_DEFAULT_CAPABILITIES_BY_TOOL,
    DelegationSnapshot,
    JsonValue,
    MessageDelivery,
    ReconciliationStatus,
    V3ExecutionGateway,
)
from misaka_coordinator_service.persistence import (
    CoordinatorSessionRecord,
    JsonlCoordinatorSessionStore,
    SessionRecordConflictError,
)
from misaka_coordinator_service.tools import (
    InMemoryToolAuditSink,
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


class CoordinatorHostRuntime:
    def __init__(self, config: CoordinatorHostConfig) -> None:
        self.config = config
        self._registry: MCPToolRegistry | None = None
        self._service: CoordinatorService | None = None
        self._start_lock: asyncio.Lock | None = None

    @property
    def service(self) -> CoordinatorService:
        if self._service is None:
            raise CoordinatorHostConfigurationError("Coordinator host has not started")
        return self._service

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
            registry = MCPToolRegistry(
                sources=(source,),
                allowed_tool_names=V3_DEFAULT_ALLOWED_TOOLS,
                audit_sink=InMemoryToolAuditSink(),
            )
            self._registry = registry
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
                    ),
                    tools=registry.create_agent_tools(),
                )
                orchestrator = CoordinatorOrchestrator(
                    agent=agent,
                    execution=V3ExecutionGateway(tools=registry),
                    config=CoordinatorOrchestratorConfig(
                        wait_timeout_ms=self.config.wait_timeout_ms,
                    ),
                )
                self._service = CoordinatorService(
                    orchestrator=orchestrator,
                    store=JsonlCoordinatorSessionStore(self.config.state_path),
                )
            except Exception:
                await registry.close()
                self._registry = None
                raise

    async def close(self) -> None:
        if self._service is not None:
            self._service.close()
        if self._registry is not None:
            await self._registry.close()
        self._service = None
        self._registry = None


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivationSubmission(_StrictModel):
    prompt: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    cognitive_session_id: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    activation_id: str | None = None


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
    }


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
        "current_activation_id": snapshot.current_activation_id,
        "current_invocation_id": snapshot.current_invocation_id,
        "next_action": snapshot.next_action,
    }
