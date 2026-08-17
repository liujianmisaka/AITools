from __future__ import annotations

import asyncio
import os
import socket

from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.worker import Worker

from multi_agent_v2.packages.agent_execution import (
    AgentActivityRunner,
    TemporalAgentActivities,
)
from multi_agent_v2.packages.agent_runtime import (
    AgentRuntimeRegistry,
    CodexRuntime,
)
from multi_agent_v2.packages.artifacts import ExecutionEvidenceService, LocalArtifactStore
from multi_agent_v2.packages.config import Settings, get_settings
from multi_agent_v2.packages.observability.logging import configure_structured_logging
from multi_agent_v2.packages.persistence import (
    ArtifactRepository,
    DatabaseManager,
    ExecutionEvidenceRepository,
    ExecutionLeaseRepository,
    WorktreeRepository,
)
from multi_agent_v2.packages.policy import (
    WorkspaceSupervisor,
    load_workspace_registry,
)
from multi_agent_v2.packages.workflow_runtime.temporal import TemporalGateway
from multi_agent_v2.packages.workflow_runtime.workflow import AGENT_TASK_QUEUE


async def serve(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    database = DatabaseManager(resolved.database_url)
    sessions = async_sessionmaker(database.engine, expire_on_commit=False)
    runtimes = AgentRuntimeRegistry(
        [
            CodexRuntime(
                network_deny_is_enforced=resolved.codex_network_deny_enforced,
            )
        ]
    )
    workspace_registry = load_workspace_registry(resolved.workspace_config_path)
    runner = AgentActivityRunner(
        executions=ExecutionLeaseRepository(sessions),
        worktrees=WorktreeRepository(sessions),
        workspaces=WorkspaceSupervisor(workspace_registry),
        runtimes=runtimes,
        evidence=ExecutionEvidenceService(
            events=ExecutionEvidenceRepository(sessions),
            artifacts=ArtifactRepository(sessions),
            store=LocalArtifactStore(resolved.artifact_root),
        ),
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    activities = TemporalAgentActivities(runner, worker_id=worker_id)
    temporal = TemporalGateway(
        address=resolved.temporal_address,
        namespace=resolved.temporal_namespace,
    )
    client = await temporal.connect()
    worker = Worker(
        client,
        task_queue=AGENT_TASK_QUEUE,
        activities=[activities.execute],
    )
    try:
        await worker.run()
    finally:
        await runtimes.aclose()
        await database.close()


def run() -> None:
    configure_structured_logging()
    asyncio.run(serve())
