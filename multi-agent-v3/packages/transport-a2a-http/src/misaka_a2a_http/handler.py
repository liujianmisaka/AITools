from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC
from typing import cast

from a2a.server.context import ServerCallContext
from a2a.server.events import Event
from a2a.server.request_handlers import RequestHandler
from a2a.types.a2a_pb2 import (
    AgentCard,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
)
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE, MAX_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import (
    ExtendedAgentCardNotConfiguredError,
    InvalidParamsError,
    PushNotificationNotSupportedError,
    TaskNotFoundError,
)
from a2a.utils.task import validate_history_length
from misaka_a2a_capability import (
    A2AError,
    TaskCapabilityRejected,
    TaskExecutionHandle,
    TaskNotFound,
)
from misaka_a2a_runtime import A2AServer

from misaka_a2a_http.mappers import (
    task_event_to_proto,
    task_request_from_proto,
    task_snapshot_to_proto,
    task_state_to_proto,
)


class SDKRequestHandler(RequestHandler):
    """Official a2a-sdk RequestHandler backed by the transport-neutral server."""

    def __init__(self, server: A2AServer) -> None:
        self._server = server

    async def on_get_task(self, params: GetTaskRequest, context: ServerCallContext) -> Task | None:
        del context
        validate_history_length(params)
        try:
            snapshot = await self._server.snapshot(params.id)
        except TaskNotFound as exc:
            raise TaskNotFoundError(message=str(exc)) from exc
        history_length = params.history_length if params.HasField("history_length") else None
        return task_snapshot_to_proto(
            snapshot,
            history_length=history_length,
            include_artifacts=True,
        )

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        del context
        validate_history_length(params)
        snapshots = list(await self._server.list_snapshots())
        if params.context_id:
            snapshots = [
                snapshot
                for snapshot in snapshots
                if snapshot.request.context_id == params.context_id
            ]
        if params.status:
            snapshots = [
                snapshot
                for snapshot in snapshots
                if task_state_to_proto(snapshot.status) == params.status
            ]
        if params.HasField("status_timestamp_after"):
            status_after = params.status_timestamp_after.ToDatetime(tzinfo=UTC)
            snapshots = [
                snapshot
                for snapshot in snapshots
                if snapshot.events and snapshot.events[-1].occurred_at > status_after
            ]
        try:
            offset = int(params.page_token or "0")
        except ValueError as exc:
            raise InvalidParamsError(message="pageToken must be an integer offset") from exc
        if offset < 0:
            raise InvalidParamsError(message="pageToken must not be negative")
        page_size = (
            params.page_size if params.HasField("page_size") else DEFAULT_LIST_TASKS_PAGE_SIZE
        )
        if not 1 <= page_size <= MAX_LIST_TASKS_PAGE_SIZE:
            raise InvalidParamsError(
                message=f"pageSize must be between 1 and {MAX_LIST_TASKS_PAGE_SIZE}"
            )
        page = snapshots[offset : offset + page_size]
        next_offset = offset + len(page)
        next_token = str(next_offset) if next_offset < len(snapshots) else ""
        history_length = params.history_length if params.HasField("history_length") else None
        include_artifacts = (
            params.include_artifacts if params.HasField("include_artifacts") else True
        )
        return ListTasksResponse(
            tasks=[
                task_snapshot_to_proto(
                    snapshot,
                    history_length=history_length,
                    include_artifacts=include_artifacts,
                )
                for snapshot in page
            ],
            next_page_token=next_token,
            page_size=len(page),
            total_size=len(snapshots),
        )

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        del context
        try:
            await self._server.cancel(params.id, "A2A client requested cancellation")
            snapshot = await self._server.snapshot(params.id)
        except TaskNotFound as exc:
            raise TaskNotFoundError(message=str(exc)) from exc
        return task_snapshot_to_proto(snapshot)

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task | Message:
        del context
        handle = await self._submit(params)
        if params.configuration.return_immediately:
            return task_snapshot_to_proto(await self._server.snapshot(handle.task_id))
        result = await handle.wait()
        return task_snapshot_to_proto(await self._server.snapshot(result.task_id))

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event, None]:
        del context
        handle = await self._submit(params)
        async for event in handle.events():
            snapshot = await self._server.snapshot(event.task_id)
            yield task_event_to_proto(
                event,
                context_id=snapshot.request.context_id,
            )

    async def on_subscribe_to_task(
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event, None]:
        start_sequence = _start_sequence(context)
        try:
            snapshot = await self._server.snapshot(params.id)
        except TaskNotFound as exc:
            raise TaskNotFoundError(message=str(exc)) from exc
        async for event in self._server.events(
            params.id,
            start_sequence=start_sequence,
        ):
            yield task_event_to_proto(
                event,
                context_id=snapshot.request.context_id,
            )

    async def on_create_task_push_notification_config(
        self,
        params: TaskPushNotificationConfig,
        context: ServerCallContext,
    ) -> TaskPushNotificationConfig:
        del params, context
        raise PushNotificationNotSupportedError

    async def on_get_task_push_notification_config(
        self,
        params: GetTaskPushNotificationConfigRequest,
        context: ServerCallContext,
    ) -> TaskPushNotificationConfig:
        del params, context
        raise PushNotificationNotSupportedError

    async def on_list_task_push_notification_configs(
        self,
        params: ListTaskPushNotificationConfigsRequest,
        context: ServerCallContext,
    ) -> ListTaskPushNotificationConfigsResponse:
        del params, context
        raise PushNotificationNotSupportedError

    async def on_delete_task_push_notification_config(
        self,
        params: DeleteTaskPushNotificationConfigRequest,
        context: ServerCallContext,
    ) -> None:
        del params, context
        raise PushNotificationNotSupportedError

    async def on_get_extended_agent_card(
        self,
        params: GetExtendedAgentCardRequest,
        context: ServerCallContext,
    ) -> AgentCard:
        del params, context
        raise ExtendedAgentCardNotConfiguredError

    async def _submit(self, params: SendMessageRequest) -> TaskExecutionHandle:
        try:
            request = task_request_from_proto(params)
            return await self._server.submit(request)
        except (TaskCapabilityRejected, ValueError) as exc:
            raise InvalidParamsError(message=str(exc)) from exc
        except A2AError as exc:
            raise InvalidParamsError(message=str(exc)) from exc


def _start_sequence(context: ServerCallContext) -> int:
    raw_headers: object = context.state.get("headers", {})
    headers: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        header_values = cast(dict[object, object], raw_headers)
        headers = {
            key: value
            for key, value in header_values.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    raw_value = headers.get("x-a2a-start-sequence")
    if raw_value is None:
        last_event_id = headers.get("last-event-id")
        if last_event_id is not None:
            try:
                return int(last_event_id) + 1
            except (TypeError, ValueError) as exc:
                raise InvalidParamsError(message="Last-Event-ID must be an integer") from exc
        return 1
    try:
        start_sequence = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise InvalidParamsError(message="X-A2A-Start-Sequence must be an integer") from exc
    if start_sequence < 1:
        raise InvalidParamsError(message="X-A2A-Start-Sequence must be positive")
    return start_sequence
