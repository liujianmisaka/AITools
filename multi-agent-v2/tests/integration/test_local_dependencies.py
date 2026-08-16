from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text

from multi_agent_v2.apps.control_api.main import create_app
from multi_agent_v2.packages.config import Settings
from multi_agent_v2.packages.observability.health import HealthReport, ReadinessStatus
from multi_agent_v2.packages.persistence import (
    CURRENT_SCHEMA_REVISION,
    DatabaseManager,
    DatabaseSchemaError,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("MULTI_AGENT_V2_RUN_INFRA_TESTS") != "1",
        reason="local infrastructure tests require an explicit opt-in",
    ),
]


@asynccontextmanager
async def _running_client(app: FastAPI) -> AsyncGenerator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


async def test_ready_with_real_local_dependencies(tmp_path: Path) -> None:
    database_url = os.environ["MULTI_AGENT_V2_DATABASE_URL"]
    temporal_address = os.environ["MULTI_AGENT_V2_TEMPORAL_ADDRESS"]
    settings = Settings(
        database_url=SecretStr(database_url),
        temporal_address=temporal_address,
        artifact_root=tmp_path / "artifacts",
        dependency_timeout_seconds=5,
    )
    app = create_app(settings)

    async with _running_client(app) as client:
        response = await client.get("/ready")

    report = HealthReport.model_validate_json(response.content)
    assert response.status_code == 200
    assert report.status is ReadinessStatus.READY
    assert {component.name for component in report.components} == {
        "artifact_root",
        "postgresql",
        "temporal",
    }


async def test_database_probe_rejects_stale_alembic_revision() -> None:
    database_url = SecretStr(os.environ["MULTI_AGENT_V2_DATABASE_URL"])
    database = DatabaseManager(database_url)
    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = 'stale_revision'")
            )
        with pytest.raises(DatabaseSchemaError, match="revision"):
            await database.check()
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": CURRENT_SCHEMA_REVISION},
            )
        await database.close()


async def test_ready_rejects_unknown_temporal_namespace(tmp_path: Path) -> None:
    settings = Settings(
        database_url=SecretStr(os.environ["MULTI_AGENT_V2_DATABASE_URL"]),
        temporal_address=os.environ["MULTI_AGENT_V2_TEMPORAL_ADDRESS"],
        temporal_namespace="namespace-that-does-not-exist",
        artifact_root=tmp_path / "artifacts",
        dependency_timeout_seconds=5,
    )
    app = create_app(settings)

    async with _running_client(app) as client:
        response = await client.get("/ready")

    report = HealthReport.model_validate_json(response.content)
    temporal = next(component for component in report.components if component.name == "temporal")
    assert response.status_code == 503
    assert report.status is ReadinessStatus.NOT_READY
    assert temporal.detail == "RPCError"
