from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.client.client import ClientCallContext
from a2a.types.a2a_pb2 import (
    CancelTaskRequest,
    GetTaskRequest,
    StreamResponse,
    SubscribeToTaskRequest,
    Task,
)
from misaka_a2a_capability import TaskRequest

from misaka_a2a_http.mappers import task_request_to_proto


class A2AHttpClient:
    """Small lifecycle-safe wrapper around the official a2a-sdk client."""

    def __init__(
        self,
        base_url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        streaming: bool = True,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        self.base_url = base_url.rstrip("/")
        self._http_client = http_client or httpx.AsyncClient()
        self._factory = ClientFactory(
            ClientConfig(
                streaming=streaming,
                httpx_client=self._http_client,
            )
        )
        self._client: Client | None = None
        self._closed = False

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("A2A client is closed")
        if self._client is not None:
            return
        try:
            self._client = await self._factory.create_from_url(self.base_url)
        except Exception:
            await self._http_client.aclose()
            self._closed = True
            raise

    async def send(self, request: TaskRequest) -> Task:
        client = self._require_client()
        final_task: Task | None = None
        async for response in client.send_message(task_request_to_proto(request)):
            if response.HasField("task"):
                final_task = Task()
                final_task.CopyFrom(response.task)
        if final_task is not None:
            return final_task
        return await client.get_task(GetTaskRequest(id=request.task_id))

    async def stream(self, request: TaskRequest) -> AsyncIterator[StreamResponse]:
        client = self._require_client()
        async for response in client.send_message(task_request_to_proto(request)):
            yield response

    async def get(self, task_id: str) -> Task:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        return await self._require_client().get_task(GetTaskRequest(id=task_id))

    async def cancel(self, task_id: str) -> Task:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        return await self._require_client().cancel_task(CancelTaskRequest(id=task_id))

    async def subscribe(
        self,
        task_id: str,
        *,
        start_sequence: int = 1,
    ) -> AsyncIterator[StreamResponse]:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        context = ClientCallContext(
            service_parameters={
                "X-A2A-Start-Sequence": str(start_sequence),
            }
        )
        async for response in self._require_client().subscribe(
            SubscribeToTaskRequest(id=task_id),
            context=context,
        ):
            yield response

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            await self._client.close()
            self._client = None
        else:
            await self._http_client.aclose()

    async def __aenter__(self) -> A2AHttpClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.close()

    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("A2A client must be connected before use")
        return self._client
