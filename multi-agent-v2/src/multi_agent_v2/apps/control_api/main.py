from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.service import RPCError

from multi_agent_v2.apps.control_api.routes import ControlApiDependencies, router
from multi_agent_v2.packages.artifacts import ArtifactRootProbe
from multi_agent_v2.packages.config import Settings, get_settings
from multi_agent_v2.packages.control_plane.catalog import DatabaseWorkflowCatalog
from multi_agent_v2.packages.control_plane.commands import WorkflowCommandService
from multi_agent_v2.packages.control_plane.schedule_adapter import ScheduleContractError
from multi_agent_v2.packages.control_plane.service import (
    ControlPlaneService,
    TriggerContractError,
    WorkflowInputContractError,
)
from multi_agent_v2.packages.credentials import (
    CredentialError,
    CredentialRef,
    LocalCredentialProvider,
)
from multi_agent_v2.packages.eventing import (
    CloudEventParseError,
    WebhookPolicy,
    WebhookVerificationError,
)
from multi_agent_v2.packages.observability import create_telemetry
from multi_agent_v2.packages.observability.health import HealthService
from multi_agent_v2.packages.observability.logging import configure_structured_logging
from multi_agent_v2.packages.persistence import (
    ControlPlaneConflict,
    ControlPlaneNotFound,
    ControlPlaneRepository,
    DatabaseManager,
    DatabaseProbe,
    IdempotencyConflict,
    RevisionConflict,
)
from multi_agent_v2.packages.policy import OriginPolicyMiddleware, load_workspace_registry
from multi_agent_v2.packages.workflow_dsl import WorkflowCompilationError
from multi_agent_v2.packages.workflow_runtime.temporal import TemporalGateway, TemporalProbe


def create_app(
    settings: Settings | None = None,
    *,
    health_service: HealthService | None = None,
    control_dependencies: ControlApiDependencies | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        database: DatabaseManager | None = None
        telemetry = create_telemetry(resolved_settings.service_name)
        app.state.telemetry = telemetry
        dependencies = control_dependencies
        if health_service is None:
            database = DatabaseManager(resolved_settings.database_url)
            temporal = TemporalGateway(
                address=resolved_settings.temporal_address,
                namespace=resolved_settings.temporal_namespace,
            )
            app.state.health_service = HealthService(
                [
                    DatabaseProbe(database),
                    TemporalProbe(temporal),
                    ArtifactRootProbe(resolved_settings.artifact_root),
                ],
                timeout_seconds=resolved_settings.dependency_timeout_seconds,
                tracer=telemetry.tracer,
            )
            sessions = async_sessionmaker(database.engine, expire_on_commit=False)
            repository = ControlPlaneRepository(sessions)
            workspaces = load_workspace_registry(resolved_settings.workspace_config_path)
            service = ControlPlaneService(
                repository=repository,
                catalog=DatabaseWorkflowCatalog(
                    repository=repository,
                    workspace_ids=workspaces.ids(),
                ),
            )
            credentials = LocalCredentialProvider(resolved_settings.credential_store_path)
            secret_ref = (
                CredentialRef(name=resolved_settings.webhook_secret_ref)
                if resolved_settings.webhook_secret_ref is not None
                else None
            )
            webhook_policy = None
            if secret_ref is not None or not resolved_settings.webhook_require_hmac:
                webhook_policy = WebhookPolicy(
                    credentials=credentials,
                    secret_ref=secret_ref,
                    require_hmac=resolved_settings.webhook_require_hmac,
                    maximum_body_bytes=resolved_settings.webhook_maximum_body_bytes,
                    timestamp_tolerance_seconds=(
                        resolved_settings.webhook_timestamp_tolerance_seconds
                    ),
                )
            dependencies = ControlApiDependencies(
                service=service,
                repository=repository,
                commands=WorkflowCommandService(
                    repository=repository,
                    temporal=temporal,
                ),
                webhook_policy=webhook_policy,
                maximum_event_bytes=resolved_settings.webhook_maximum_body_bytes,
            )
        else:
            app.state.health_service = health_service
        app.state.control_dependencies = dependencies

        try:
            yield
        finally:
            if database is not None:
                await database.close()
            telemetry.shutdown()

    app = FastAPI(
        title="Multi-Agent Platform V2 Control API",
        version="0.1.0",
        lifespan=lifespan,
    )
    allowed_hosts = list(
        dict.fromkeys([*resolved_settings.allowed_hosts, resolved_settings.control_host])
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )
    app.add_middleware(
        OriginPolicyMiddleware,
        allowed_origins=resolved_settings.allowed_origins,
    )
    _install_exception_handlers(app)
    app.include_router(router)
    return app


def _install_exception_handlers(app: FastAPI) -> None:
    async def _not_found(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": type(exc).__name__},
        )

    async def _conflict(_: Request, exc: Exception) -> JSONResponse:
        code = (
            "idempotency_conflict"
            if isinstance(exc, IdempotencyConflict)
            else "revision_conflict"
            if isinstance(exc, RevisionConflict)
            else "control_plane_conflict"
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": code},
        )

    async def _compilation_error(_: Request, exc: Exception) -> JSONResponse:
        error = cast(WorkflowCompilationError, exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": "workflow_compilation_failed",
                "issues": [issue.model_dump(mode="json", by_alias=True) for issue in error.issues],
            },
        )

    app.add_exception_handler(ControlPlaneNotFound, _not_found)
    app.add_exception_handler(ControlPlaneConflict, _conflict)
    app.add_exception_handler(WorkflowCompilationError, _compilation_error)

    invalid_types = (
        WorkflowInputContractError,
        TriggerContractError,
        ScheduleContractError,
        CloudEventParseError,
        WebhookVerificationError,
    )
    for exception_type in invalid_types:

        async def _invalid_request(
            _: Request,
            exc: Exception,
            *,
            _name: str = exception_type.__name__,
        ) -> JSONResponse:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": _name},
            )

        app.add_exception_handler(exception_type, _invalid_request)

    async def _temporal_unavailable(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": type(exc).__name__},
        )

    app.add_exception_handler(RPCError, _temporal_unavailable)
    app.add_exception_handler(CredentialError, _temporal_unavailable)


app = create_app()


def run() -> None:
    settings = get_settings()
    configure_structured_logging()
    uvicorn.run(
        "multi_agent_v2.apps.control_api.main:app",
        host=settings.control_host,
        port=settings.control_port,
        reload=False,
    )
