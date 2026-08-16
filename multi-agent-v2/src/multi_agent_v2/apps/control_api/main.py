from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from multi_agent_v2.apps.control_api.routes import router
from multi_agent_v2.packages.artifacts import ArtifactRootProbe
from multi_agent_v2.packages.config import Settings, get_settings
from multi_agent_v2.packages.observability import create_telemetry
from multi_agent_v2.packages.observability.health import HealthService
from multi_agent_v2.packages.observability.logging import configure_structured_logging
from multi_agent_v2.packages.persistence import DatabaseManager, DatabaseProbe
from multi_agent_v2.packages.policy import OriginPolicyMiddleware
from multi_agent_v2.packages.workflow_runtime.temporal import TemporalGateway, TemporalProbe


def create_app(
    settings: Settings | None = None,
    *,
    health_service: HealthService | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        database: DatabaseManager | None = None
        telemetry = create_telemetry(resolved_settings.service_name)
        app.state.telemetry = telemetry
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
        else:
            app.state.health_service = health_service

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
    app.include_router(router)
    return app


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
