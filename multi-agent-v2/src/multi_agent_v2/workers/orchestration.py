from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.worker import Worker

from multi_agent_v2.packages.config import Settings, get_settings
from multi_agent_v2.packages.control_plane.activities import TemporalControlActivities
from multi_agent_v2.packages.eventing import GitRefPoller
from multi_agent_v2.packages.observability.logging import configure_structured_logging
from multi_agent_v2.packages.persistence import ControlPlaneRepository, DatabaseManager
from multi_agent_v2.packages.policy import load_workspace_registry
from multi_agent_v2.packages.workflow_runtime.connector_workflow import GitConnectorWorkflow
from multi_agent_v2.packages.workflow_runtime.schedule_workflow import ScheduleTriggerWorkflow
from multi_agent_v2.packages.workflow_runtime.temporal import TemporalGateway
from multi_agent_v2.packages.workflow_runtime.workflow import (
    ORCHESTRATION_TASK_QUEUE,
    WorkflowInstanceWorkflow,
)


async def serve(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    database = DatabaseManager(resolved.database_url)
    sessions = async_sessionmaker(database.engine, expire_on_commit=False)
    repository = ControlPlaneRepository(sessions)
    workspaces = load_workspace_registry(resolved.workspace_config_path)
    activities = TemporalControlActivities(
        repository=repository,
        git_poller=GitRefPoller(repository=repository, workspaces=workspaces),
    )
    temporal = TemporalGateway(
        address=resolved.temporal_address,
        namespace=resolved.temporal_namespace,
    )
    client = await temporal.connect()
    worker = Worker(
        client,
        task_queue=ORCHESTRATION_TASK_QUEUE,
        workflows=[WorkflowInstanceWorkflow, GitConnectorWorkflow, ScheduleTriggerWorkflow],
        activities=[
            activities.register_event_wait,
            activities.close_event_wait,
            activities.publish_projection,
            activities.ingest_cloud_event,
            activities.fire_schedule,
            activities.poll_git,
            activities.execute_registered,
        ],
    )
    try:
        await worker.run()
    finally:
        await database.close()


def run() -> None:
    configure_structured_logging()
    asyncio.run(serve())
