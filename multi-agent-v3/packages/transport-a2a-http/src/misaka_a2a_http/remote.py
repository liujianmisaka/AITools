from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from a2a.types.a2a_pb2 import StreamResponse, Task
from misaka_a2a_capability import (
    A2AAgentCard,
    RemoteTaskClient,
    TaskEvent,
    TaskExecutionHandle,
    TaskIdempotencyConflict,
    TaskRequest,
    TaskResult,
    TaskSnapshot,
    task_request_fingerprint,
)

from misaka_a2a_http.client import A2AHttpClient
from misaka_a2a_http.mappers import (
    agent_card_from_proto,
    task_event_from_proto,
    task_result_from_proto,
    task_snapshot_from_proto,
)


class A2AHttpTaskClient(RemoteTaskClient):
    """Adapt the official HTTP/SSE client to the neutral RemoteTaskClient port."""

    def __init__(
        self,
        client: A2AHttpClient,
        *,
        first_response_timeout_seconds: float = 30.0,
    ) -> None:
        if first_response_timeout_seconds <= 0:
            raise ValueError("first_response_timeout_seconds must be positive")
        self.client = client
        self.first_response_timeout_seconds = first_response_timeout_seconds
        self._requests: dict[str, TaskRequest] = {}
        self._handles: dict[str, _A2AHttpTaskHandle] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def describe(self) -> A2AAgentCard:
        self._ensure_open()
        await self.client.connect()
        return agent_card_from_proto(self.client.card)

    async def submit(self, request: TaskRequest) -> TaskExecutionHandle:
        self._ensure_open()
        await self.client.connect()
        async with self._lock:
            existing = self._handles.get(request.task_id)
            if existing is not None:
                existing_request = self._requests[request.task_id]
                if task_request_fingerprint(existing_request) != task_request_fingerprint(request):
                    raise TaskIdempotencyConflict(
                        "a2a.task_idempotency_conflict",
                        f"task {request.task_id} was submitted with different content",
                    )
                return existing
            self._requests[request.task_id] = request
            handle = _A2AHttpTaskHandle(self, request)
            self._handles[request.task_id] = handle
        try:
            await handle.start()
        except Exception:
            async with self._lock:
                self._handles.pop(request.task_id, None)
            raise
        return handle

    async def get(self, task_id: str) -> TaskSnapshot:
        self._ensure_open()
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        await self.client.connect()
        task = await self.client.get(task_id)
        return task_snapshot_from_proto(task, request=self._requests.get(task_id))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handles = tuple(self._handles.values())
        await asyncio.gather(*(handle.close() for handle in handles), return_exceptions=True)
        self._handles.clear()
        self._requests.clear()
        await self.client.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("A2A HTTP task client is closed")


class _A2AHttpTaskHandle(TaskExecutionHandle):
    def __init__(self, owner: A2AHttpTaskClient, request: TaskRequest) -> None:
        self._owner = owner
        self._request = request
        self._events: list[TaskEvent] = []
        self._condition = asyncio.Condition()
        self._result: TaskResult | None = None
        self._error: BaseException | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._first_response = asyncio.Event()
        self._closed = False

    @property
    def task_id(self) -> str:
        return self._request.task_id

    @property
    def invocation_id(self) -> None:
        return None

    @property
    def delegation_id(self) -> None:
        return None

    @property
    def activation_id(self) -> None:
        return None

    async def start(self) -> None:
        self._stream_task = asyncio.create_task(
            self._consume_stream(),
            name=f"a2a-http-task:{self.task_id}",
        )
        try:
            async with asyncio.timeout(self._owner.first_response_timeout_seconds):
                await self._first_response.wait()
        except TimeoutError as exc:
            await self.close()
            raise TimeoutError(
                f"remote A2A task {self.task_id} produced no response before the deadline"
            ) from exc

    async def events(self, *, start_sequence: int = 1) -> AsyncIterator[TaskEvent]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        index = start_sequence - 1
        while True:
            async with self._condition:
                while index >= len(self._events) and self._error is None and self._result is None:
                    await self._condition.wait()
                if index < len(self._events):
                    event = self._events[index]
                    index += 1
                else:
                    error = self._error
                    if error is not None:
                        raise error
                    return
            yield event

    async def wait(self) -> TaskResult:
        while True:
            async with self._condition:
                if self._result is not None:
                    return self._result
                if self._error is not None:
                    raise self._error
                await self._condition.wait()

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        task = await self._owner.client.cancel(self.task_id)
        await self._record_task(task)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._stream_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._condition:
            if self._result is None and self._error is None:
                self._error = RuntimeError(f"A2A task handle {self.task_id} was closed")
            self._condition.notify_all()

    async def _consume_stream(self) -> None:
        try:
            async for response in self._owner.client.stream(self._request):
                self._first_response.set()
                await self._record_response(response)
            if self._result is None and self._error is None:
                task = await self._owner.client.get(self.task_id)
                self._first_response.set()
                await self._record_task(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._condition:
                self._error = exc
                self._first_response.set()
                self._condition.notify_all()

    async def _record_response(self, response: StreamResponse) -> None:
        event = task_event_from_proto(
            response,
            task_id=self.task_id,
            fallback_sequence=len(self._events) + 1,
        )
        if event is not None:
            await self._record_event(event)
        if response.HasField("task"):
            await self._record_task(response.task)

    async def _record_event(self, event: TaskEvent) -> None:
        async with self._condition:
            if event.sequence <= len(self._events):
                return
            if event.sequence != len(self._events) + 1:
                self._error = RuntimeError(
                    f"remote A2A task {self.task_id} emitted a non-contiguous sequence"
                )
                self._condition.notify_all()
                return
            self._events.append(event)
            self._condition.notify_all()

    async def _record_task(self, task: Task) -> None:
        result = task_result_from_proto(task)
        if result is None:
            return
        async with self._condition:
            if self._error is not None:
                self._condition.notify_all()
                return
            if self._result is None:
                self._result = result
            self._condition.notify_all()
