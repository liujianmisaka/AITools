from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast

from misaka_agent_host_profile import AgentHost
from misaka_coordinator_temporal import (
    TemporalCoordinator,
    TemporalInvocationInput,
    build_temporal_worker,
)
from misaka_invocation_contracts import InvocationRequest, InvocationResult
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableEventStore
from misaka_persistence_postgres import PostgresDurableStore


class DurableWorker(Protocol):
    async def run(self) -> None: ...

    async def shutdown(self) -> None: ...


class DurableExecution(Protocol):
    @property
    def workflow_id(self) -> str: ...

    @property
    def run_id(self) -> str | None: ...

    async def wait(self) -> InvocationResult: ...

    async def cancel(self) -> None: ...


class DurableCoordinator(Protocol):
    async def start(
        self,
        workflow_id: str,
        input: TemporalInvocationInput,
    ) -> DurableExecution: ...


class DurableStoreResource(DurableEventStore, Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DurableAgentConfig:
    task_queue: str = "misaka-agent"
    audit_stream_prefix: str = "durable-agent"
    shutdown_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.task_queue.strip() or not self.audit_stream_prefix.strip():
            raise ValueError("task_queue and audit_stream_prefix must not be empty")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")


class DurableAgentProfile:
    """Temporal owns execution truth; PostgreSQL stores append-only audit facts."""

    def __init__(
        self,
        agent_host: AgentHost,
        store: DurableStoreResource,
        coordinator: DurableCoordinator,
        worker: DurableWorker,
        *,
        config: DurableAgentConfig | None = None,
    ) -> None:
        self.agent_host = agent_host
        self.store = store
        self.coordinator = coordinator
        self.worker = worker
        self.config = config or DurableAgentConfig()
        self._worker_task: asyncio.Task[None] | None = None
        self._started = False
        self._lifecycle_lock = asyncio.Lock()

    @classmethod
    def from_temporal(
        cls,
        agent_host: AgentHost,
        store: PostgresDurableStore,
        client: object,
        *,
        config: DurableAgentConfig | None = None,
    ) -> DurableAgentProfile:
        settings = config or DurableAgentConfig()
        temporal_coordinator = TemporalCoordinator(
            cast(Any, client), task_queue=settings.task_queue
        )
        worker = build_temporal_worker(
            cast(Any, client),
            agent_host.runtime,
            task_queue=settings.task_queue,
        )
        return cls(agent_host, store, temporal_coordinator, worker, config=settings)

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            try:
                await self.store.start()
                await self.agent_host.start()
                self._worker_task = asyncio.create_task(self.worker.run())
                self._started = True
            except Exception:
                await self._stop_worker()
                await self.agent_host.stop()
                await self.store.close()
                raise

    async def submit(
        self,
        request: InvocationRequest,
        *,
        workflow_id: str | None = None,
        provider_id: str | None = None,
        maximum_attempts: int = 1,
    ) -> DurableExecutionHandle:
        self._require_started()
        selected_workflow_id = workflow_id or request.invocation_id
        input = TemporalInvocationInput.from_request(
            request,
            provider_id=provider_id,
            maximum_attempts=maximum_attempts,
        )
        stream_id = self._stream_id(request.invocation_id)
        await self.store.append(
            stream_id,
            "accepted",
            "durable.invocation.accepted",
            _input_payload(input),
        )
        execution = await self.coordinator.start(selected_workflow_id, input)
        await self.store.append(
            stream_id,
            "started",
            "durable.invocation.started",
            {
                "invocation_id": request.invocation_id,
                "workflow_id": execution.workflow_id,
                "run_id": execution.run_id,
            },
        )
        return DurableExecutionHandle(self, execution, stream_id, request.invocation_id)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._started:
                return
            try:
                await self._stop_worker()
            finally:
                self._started = False
                await self.agent_host.stop()
                await self.store.close()

    async def _stop_worker(self) -> None:
        task = self._worker_task
        self._worker_task = None
        if task is None:
            return
        try:
            async with asyncio.timeout(self.config.shutdown_timeout_seconds):
                await self.worker.shutdown()
                await task
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("durable agent profile must be started before use")

    def _stream_id(self, invocation_id: str) -> str:
        return f"{self.config.audit_stream_prefix}:{invocation_id}"


class DurableExecutionHandle:
    def __init__(
        self,
        profile: DurableAgentProfile,
        execution: DurableExecution,
        stream_id: str,
        invocation_id: str,
    ) -> None:
        self._profile = profile
        self._execution = execution
        self._stream_id = stream_id
        self.invocation_id = invocation_id
        self._wait_lock = asyncio.Lock()
        self._result: InvocationResult | None = None

    @property
    def workflow_id(self) -> str:
        return self._execution.workflow_id

    @property
    def run_id(self) -> str | None:
        return self._execution.run_id

    async def wait(self) -> InvocationResult:
        async with self._wait_lock:
            if self._result is None:
                self._result = await self._execution.wait()
                await self._profile.store.append(
                    self._stream_id,
                    "completed",
                    "durable.invocation.completed",
                    _result_payload(self._result),
                )
            return self._result

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        await self._profile.store.append(
            self._stream_id,
            "cancel-requested",
            "durable.invocation.cancel_requested",
            {"invocation_id": self.invocation_id, "reason": reason},
        )
        await self._execution.cancel()


def _input_payload(input: TemporalInvocationInput) -> JsonObject:
    payload = cast(JsonObject, asdict(input))
    payload["required_features"] = list(input.required_features)
    return payload


def _result_payload(result: InvocationResult) -> JsonObject:
    payload: JsonObject = {
        "invocation_id": result.invocation_id,
        "status": result.status.value,
    }
    if result.output is not None:
        payload["output"] = result.output
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    if result.error_message is not None:
        payload["error_message"] = result.error_message
    return payload
