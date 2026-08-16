from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from temporalio.client import ScheduleUpdate, ScheduleUpdateInput
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from multi_agent_v2.packages.control_plane.models import (
    OutboxCommand,
    ScheduleRecord,
)
from multi_agent_v2.packages.control_plane.schedule_adapter import build_temporal_schedule
from multi_agent_v2.packages.domain.events import CloudEventEnvelope
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.persistence import ControlPlaneRepository, OutboxLeaseLost
from multi_agent_v2.packages.workflow_runtime.messages import (
    EventCommand,
    WorkflowRunInput,
)
from multi_agent_v2.packages.workflow_runtime.temporal import TemporalGateway
from multi_agent_v2.packages.workflow_runtime.workflow import (
    ORCHESTRATION_TASK_QUEUE,
    WorkflowInstanceWorkflow,
)


class _StartPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instanceId: str
    templateId: str
    templateVersion: int
    temporalWorkflowId: str
    workflowInput: JsonObject


class _SignalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instanceId: str
    temporalWorkflowId: str
    signalName: str
    commandId: str
    nodeId: str
    activation: int
    event: CloudEventEnvelope


class CommandTransport(Protocol):
    async def dispatch(self, command: OutboxCommand) -> None: ...


class TemporalCommandTransport:
    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        temporal: TemporalGateway,
    ) -> None:
        self._repository = repository
        self._temporal = temporal

    async def dispatch(self, command: OutboxCommand) -> None:
        if command.command_type == "workflow.start.v1":
            await self._start_workflow(command)
            return
        if command.command_type == "workflow.signal.v1":
            await self._signal_workflow(command)
            return
        if command.command_type == "schedule.sync.v1":
            await self._sync_schedule(command)
            return
        raise ValueError(f"unsupported outbox command type: {command.command_type}")

    async def _start_workflow(self, command: OutboxCommand) -> None:
        payload = _StartPayload.model_validate(command.payload)
        version = await self._repository.get_template_version(
            payload.templateId,
            payload.templateVersion,
        )
        client = await self._temporal.connect()
        try:
            await client.start_workflow(
                WorkflowInstanceWorkflow.run,
                WorkflowRunInput(
                    plan=version.compiled_plan,
                    workflow_input=payload.workflowInput,
                ),
                id=payload.temporalWorkflowId,
                task_queue=ORCHESTRATION_TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                static_summary=f"instance:{payload.instanceId}",
            )
        except WorkflowAlreadyStartedError:
            return

    async def _signal_workflow(self, command: OutboxCommand) -> None:
        payload = _SignalPayload.model_validate(command.payload)
        client = await self._temporal.connect()
        handle = client.get_workflow_handle(payload.temporalWorkflowId)
        await handle.signal(
            payload.signalName,
            EventCommand(
                command_id=payload.commandId,
                node_id=payload.nodeId,
                activation=payload.activation,
                event=payload.event,
            ),
        )

    async def _sync_schedule(self, command: OutboxCommand) -> None:
        record = ScheduleRecord.model_validate(command.payload)
        schedule = build_temporal_schedule(record)
        client = await self._temporal.connect()
        handle = client.get_schedule_handle(record.schedule_id)
        try:
            await handle.describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            try:
                await client.create_schedule(record.schedule_id, schedule)
            except RPCError as create_error:
                if create_error.status != RPCStatusCode.ALREADY_EXISTS:
                    raise
            else:
                return

        async def _replace(update: ScheduleUpdateInput) -> ScheduleUpdate | None:
            current_revision = _managed_schedule_revision(update.description.schedule.state.note)
            if current_revision is None:
                raise ValueError("Temporal schedule is not managed by multi-agent-v2")
            if current_revision >= record.revision:
                return None
            return ScheduleUpdate(schedule)

        await handle.update(_replace)


class CommandDispatcher:
    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        transport: CommandTransport,
        lease_owner: str,
        lease_duration: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=5),
        maximum_attempts: int = 20,
    ) -> None:
        self._repository = repository
        self._transport = transport
        self._lease_owner = lease_owner
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._maximum_attempts = maximum_attempts

    async def run_once(self, *, limit: int = 20) -> int:
        commands = await self._repository.claim_outbox(
            lease_owner=self._lease_owner,
            lease_duration=self._lease_duration,
            limit=limit,
        )
        async with asyncio.TaskGroup() as group:
            for command in commands:
                group.create_task(self._dispatch_one(command))
        return len(commands)

    async def _dispatch_one(self, command: OutboxCommand) -> None:
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._renew_lease(command, lease_lost))
        dispatch_error: Exception | None = None
        try:
            await self._transport.dispatch(command)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            dispatch_error = exc
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                lease_lost.set()

        if lease_lost.is_set():
            return
        try:
            if dispatch_error is not None:
                await self._repository.fail_outbox(
                    command,
                    error=type(dispatch_error).__name__,
                    retry_delay=self._retry_delay,
                    maximum_attempts=self._maximum_attempts,
                )
            else:
                await self._repository.complete_outbox(command)
        except OutboxLeaseLost:
            return

    async def _renew_lease(
        self,
        command: OutboxCommand,
        lease_lost: asyncio.Event,
    ) -> None:
        interval = min(self._lease_duration.total_seconds() / 3, 5.0)
        while True:
            await asyncio.sleep(interval)
            renewed = await self._repository.renew_outbox(
                command,
                lease_duration=self._lease_duration,
            )
            if not renewed:
                lease_lost.set()
                return


def _managed_schedule_revision(note: str | None) -> int | None:
    prefix = "managed by multi-agent-v2 revision "
    if note is None or not note.startswith(prefix):
        return None
    try:
        revision = int(note.removeprefix(prefix))
    except ValueError:
        return None
    return revision if revision > 0 else None
