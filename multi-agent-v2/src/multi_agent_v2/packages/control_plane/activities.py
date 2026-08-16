from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from temporalio import activity
from temporalio.exceptions import ApplicationError

from multi_agent_v2.packages.control_plane.models import (
    EventWaitRegistration,
    ProjectionEvent,
    ScheduleFireRequest,
)
from multi_agent_v2.packages.domain.events import CloudEventEnvelope, EventIngestResult
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.eventing.git_connector import GitRefPoller
from multi_agent_v2.packages.persistence import ControlPlaneRepository
from multi_agent_v2.packages.workflow_dsl.ir import ActivityExecutionIr
from multi_agent_v2.packages.workflow_runtime.activities import successful_activity_result
from multi_agent_v2.packages.workflow_runtime.messages import (
    EventWaitCloseRequest,
    EventWaitSubscriptionRequest,
    NodeActivityRequest,
    NodeActivityResult,
    ProjectionEventRequest,
)

type RegisteredActivityHandler = Callable[[JsonObject], Awaitable[JsonObject]]


class RegisteredActivityRegistry:
    def __init__(
        self,
        handlers: Mapping[tuple[str, int], RegisteredActivityHandler] | None = None,
    ) -> None:
        self._handlers = dict(handlers or {})

    def get(self, name: str, version: int) -> RegisteredActivityHandler:
        try:
            return self._handlers[(name, version)]
        except KeyError as exc:
            raise ApplicationError(
                "registered activity is not available on this worker",
                type="RegisteredActivityUnavailable",
                non_retryable=True,
            ) from exc


class TemporalControlActivities:
    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        git_poller: GitRefPoller | None = None,
        registered_activities: RegisteredActivityRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._git_poller = git_poller
        self._registered_activities = registered_activities or RegisteredActivityRegistry()

    @activity.defn(name="event-wait.register.v1")
    async def register_event_wait(self, request: EventWaitSubscriptionRequest) -> None:
        await self._repository.register_event_wait(
            EventWaitRegistration(
                subscription_id=request.subscription_id,
                instance_id=request.instance_id,
                temporal_workflow_id=request.temporal_workflow_id,
                node_id=request.node_id,
                activation=request.activation,
                event_type=request.event_type,
                source_pattern=request.source_pattern,
                subject_pattern=request.subject_pattern,
                correlation_key=request.correlation_key,
                output_schema=request.output_schema,
                expires_at=request.expires_at,
            )
        )

    @activity.defn(name="event-wait.close.v1")
    async def close_event_wait(self, request: EventWaitCloseRequest) -> None:
        await self._repository.close_event_wait(
            instance_id=request.instance_id,
            node_id=request.node_id,
            activation=request.activation,
        )

    @activity.defn(name="projection.publish.v1")
    async def publish_projection(self, request: ProjectionEventRequest) -> bool:
        return await self._repository.publish_projection(
            ProjectionEvent(
                event_id=request.event_id,
                instance_id=request.instance_id,
                event_type=request.event_type,
                data=request.data,
                occurred_at=request.occurred_at,
            )
        )

    @activity.defn(name="cloud-event.ingest.v1")
    async def ingest_cloud_event(self, event: CloudEventEnvelope) -> EventIngestResult:
        return await self._repository.ingest_event(event)

    @activity.defn(name="schedule.fire.v1")
    async def fire_schedule(self, request: ScheduleFireRequest) -> JsonObject:
        instance = await self._repository.fire_schedule(request)
        if instance is None:
            return {"accepted": False, "instanceId": None}
        return {"accepted": True, "instanceId": instance.instance_id}

    @activity.defn(name="git.poll.v1")
    async def poll_git(self, target: object) -> JsonObject:
        if self._git_poller is None:
            raise ApplicationError(
                "Git connector is not configured on this worker",
                type="GitConnectorUnavailable",
                non_retryable=True,
            )
        from multi_agent_v2.packages.control_plane.models import GitRefTarget

        parsed = GitRefTarget.model_validate(target)
        result = await self._git_poller.poll(parsed)
        return {
            "initialized": result.initialized,
            "changed": result.changed,
            "previousCommit": result.previous_commit,
            "currentCommit": result.current_commit,
            "eventId": result.event.id if result.event is not None else None,
            "inboxId": result.ingest.inbox_id if result.ingest is not None else None,
        }

    @activity.defn(name="registered-activity.execute.v1")
    async def execute_registered(self, request: NodeActivityRequest) -> NodeActivityResult:
        execution = request.execution
        if not isinstance(execution, ActivityExecutionIr):
            raise ApplicationError(
                "registered activity received an incompatible node contract",
                type="RegisteredActivityContractInvalid",
                non_retryable=True,
            )
        handler = self._registered_activities.get(execution.name, execution.version)
        output = await handler(request.resolved_inputs)
        return successful_activity_result(request, output)
