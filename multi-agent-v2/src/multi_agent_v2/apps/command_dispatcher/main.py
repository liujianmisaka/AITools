from __future__ import annotations

import asyncio
import os
import socket

from sqlalchemy.ext.asyncio import async_sessionmaker

from multi_agent_v2.apps.command_dispatcher.dispatcher import (
    CommandDispatcher,
    TemporalCommandTransport,
)
from multi_agent_v2.packages.config import Settings, get_settings
from multi_agent_v2.packages.observability.logging import configure_structured_logging
from multi_agent_v2.packages.persistence import ControlPlaneRepository, DatabaseManager
from multi_agent_v2.packages.workflow_runtime.temporal import TemporalGateway


async def serve(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    database = DatabaseManager(resolved.database_url)
    sessions = async_sessionmaker(database.engine, expire_on_commit=False)
    repository = ControlPlaneRepository(sessions)
    temporal = TemporalGateway(
        address=resolved.temporal_address,
        namespace=resolved.temporal_namespace,
    )
    dispatcher = CommandDispatcher(
        repository=repository,
        transport=TemporalCommandTransport(repository=repository, temporal=temporal),
        lease_owner=f"{socket.gethostname()}:{os.getpid()}",
    )
    try:
        while True:
            claimed = await dispatcher.run_once()
            if claimed == 0:
                await asyncio.sleep(0.5)
    finally:
        await database.close()


def run() -> None:
    configure_structured_logging()
    asyncio.run(serve())
