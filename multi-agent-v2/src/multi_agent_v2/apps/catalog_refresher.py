from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from multi_agent_v2.packages.agent_runtime import CodexRuntime
from multi_agent_v2.packages.config import Settings, get_settings
from multi_agent_v2.packages.control_plane.models import CatalogModelRecord
from multi_agent_v2.packages.observability.logging import configure_structured_logging
from multi_agent_v2.packages.persistence import ControlPlaneRepository, DatabaseManager


async def refresh_once(
    repository: ControlPlaneRepository,
    *,
    network_deny_is_enforced: bool,
) -> None:
    runtime = CodexRuntime(network_deny_is_enforced=network_deny_is_enforced)
    try:
        catalog = await runtime.list_models(refresh=True)
        await repository.save_provider_catalog(
            runtime_name=catalog.runtime_name,
            runtime_id=catalog.runtime_id,
            provider_id=catalog.provider_id,
            revision=catalog.revision,
            models=tuple(
                CatalogModelRecord.model_validate(model.model_dump(mode="json"))
                for model in catalog.models
            ),
        )
    finally:
        await runtime.aclose()


async def serve(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    database = DatabaseManager(resolved.database_url)
    sessions = async_sessionmaker(database.engine, expire_on_commit=False)
    repository = ControlPlaneRepository(sessions)
    try:
        while True:
            await refresh_once(
                repository,
                network_deny_is_enforced=resolved.codex_network_deny_enforced,
            )
            await asyncio.sleep(resolved.catalog_refresh_seconds)
    finally:
        await database.close()


def run() -> None:
    configure_structured_logging()
    asyncio.run(serve())
