from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
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
from multi_agent_v2.packages.config import Settings, get_settings
from multi_agent_v2.packages.observability.logging import configure_structured_logging
from multi_agent_v2.packages.persistence import (
    DatabaseManager,
    ExecutionLeaseRepository,
    WorktreeRepository,
)
from multi_agent_v2.packages.policy import (
    WorkspaceDefinition,
    WorkspaceRegistry,
    WorkspaceSupervisor,
)
from multi_agent_v2.packages.workflow_runtime.temporal import TemporalGateway
from multi_agent_v2.packages.workflow_runtime.workflow import AGENT_TASK_QUEUE


class _WorkspaceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    workspace_id: str = Field(alias="id", min_length=1, max_length=64)
    root: Path
    worktree_root: Path = Field(alias="worktreeRoot")
    base_ref: str = Field(default="HEAD", alias="baseRef")


class _WorkspaceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaces: tuple[_WorkspaceEntry, ...]


def load_workspace_registry(path: Path) -> WorkspaceRegistry:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        config = _WorkspaceFile.model_validate(raw)
    except FileNotFoundError as exc:
        raise RuntimeError(f"workspace configuration does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise RuntimeError(f"workspace configuration is invalid: {path}") from exc

    base_directory = path.parent
    definitions: list[WorkspaceDefinition] = []
    for entry in config.workspaces:
        root = entry.root if entry.root.is_absolute() else base_directory / entry.root
        worktree_root = (
            entry.worktree_root
            if entry.worktree_root.is_absolute()
            else base_directory / entry.worktree_root
        )
        definitions.append(
            WorkspaceDefinition(
                workspace_id=entry.workspace_id,
                root=root.resolve(),
                worktree_root=worktree_root.resolve(),
                base_ref=entry.base_ref,
            )
        )
    return WorkspaceRegistry(definitions)


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
