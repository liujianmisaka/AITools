from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from multi_agent.api.routes import create_router
from multi_agent.domain.errors import (
    ApprovalNotFoundError,
    ApprovalStateError,
    CoordinatorContractError,
    CoordinatorOutputError,
    CoordinatorUnavailableError,
    OrchestrationError,
    ProviderNotFoundError,
    RunNotFoundError,
    WorkspaceNotAllowedError,
)
from multi_agent.orchestration.engine import WorkflowEngine
from multi_agent.providers.claude import ClaudeProvider
from multi_agent.providers.codex import CodexProvider
from multi_agent.providers.copilot import CopilotProvider
from multi_agent.providers.registry import ProviderRegistry
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.workspaces.manager import WorkspaceManager


def _default_workspaces() -> dict[str, str]:
    raw = os.getenv("MULTI_AGENT_WORKSPACES")
    if raw:
        value = json.loads(raw)
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(path, str)
            for key, path in value.items()
        ):
            raise ValueError("MULTI_AGENT_WORKSPACES must be a JSON object of string paths")
        return value
    return {"default": str(Path.cwd())}


def create_default_engine() -> WorkflowEngine:
    project_dir = Path(__file__).resolve().parents[1]
    codex_home = Path(
        os.getenv("MULTI_AGENT_CODEX_HOME", Path.home() / ".codex")
    )
    state_db = Path(
        os.getenv("MULTI_AGENT_STATE_DB", project_dir / "data" / "state.sqlite3")
    )
    registry = ProviderRegistry(
        [
            CodexProvider(
                codex_bin=os.getenv("MULTI_AGENT_CODEX_BIN") or None,
                codex_home=str(codex_home),
            ),
            ClaudeProvider(),
            CopilotProvider(),
        ]
    )
    return WorkflowEngine(
        store=SQLiteStore(state_db),
        providers=registry,
        workspaces=WorkspaceManager(_default_workspaces()),
    )


def create_app(
    engine: WorkflowEngine | None = None,
) -> FastAPI:
    service_engine = engine or create_default_engine()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await service_engine.start()
        try:
            yield
        finally:
            await service_engine.close()

    app = FastAPI(
        title="Multi-Agent Orchestrator",
        version="0.3.0",
        description=(
            "Deterministic orchestration for coding-agent SDKs. The reserved Pi "
            "contract-advisor interface is not wired into the runtime."
        ),
        lifespan=lifespan,
    )
    app.state.engine = service_engine

    @app.exception_handler(OrchestrationError)
    async def orchestration_error_handler(
        _request: Request,
        exc: OrchestrationError,
    ) -> JSONResponse:
        if isinstance(
            exc,
            (RunNotFoundError, ApprovalNotFoundError, ProviderNotFoundError),
        ):
            status_code = 404
        elif isinstance(
            exc,
            (ApprovalStateError, WorkspaceNotAllowedError),
        ):
            status_code = 409
        elif isinstance(exc, CoordinatorUnavailableError):
            status_code = 503
        elif isinstance(exc, (CoordinatorContractError, CoordinatorOutputError)):
            status_code = 422
        else:
            status_code = 400
        return JSONResponse(
            status_code=status_code,
            content={"detail": str(exc), "code": getattr(exc, "code", "error")},
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(create_router(service_engine))
    return app


app = create_app()
