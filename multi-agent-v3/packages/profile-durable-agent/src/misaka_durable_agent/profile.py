from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol, cast

from misaka_agent_host_profile import AgentHost
from misaka_coordinator_runtime import ExecutionHandle, ExecutionResult, ExecutionStatus
from misaka_coordinator_temporal import (
    TemporalCoordinator,
    TemporalExecutionPlan,
    TemporalInvocationInput,
    build_temporal_worker,
)
from misaka_invocation_contracts import InvocationRequest, InvocationResult, InvocationStatus
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableEventStore
from misaka_persistence_postgres import PostgresDurableStore
from temporalio.client import Client


class DurableWorker(Protocol):
    @property
    def is_running(self) -> bool: ...

    async def run(self) -> None: ...

    async def shutdown(self) -> None: ...


class DurableExecution(ExecutionHandle, Protocol):
    @property
    def workflow_id(self) -> str: ...

    @property
    def run_id(self) -> str | None: ...


class DurableCoordinator(Protocol):
    async def submit(
        self,
        plan: TemporalExecutionPlan,
    ) -> DurableExecution: ...


DurablePlanFactory = Callable[[TemporalInvocationInput, str], TemporalExecutionPlan]


class DurableStoreResource(DurableEventStore, Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DurableAgentConfig:
    task_queue: str = "misaka-agent"
    audit_stream_prefix: str = "durable-agent"
    worker_start_timeout_seconds: float = 15.0
    shutdown_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.task_queue.strip() or not self.audit_stream_prefix.strip():
            raise ValueError("task_queue and audit_stream_prefix must not be empty")
        if self.worker_start_timeout_seconds <= 0 or self.shutdown_timeout_seconds <= 0:
            raise ValueError("worker timeout values must be positive")


class DurableAgentProfile:
    """Temporal owns execution truth; PostgreSQL stores append-only audit facts."""

    def __init__(
        self,
        agent_host: AgentHost,
        store: DurableStoreResource,
        coordinator: DurableCoordinator,
        worker: DurableWorker,
        *,
        plan_factory: DurablePlanFactory,
        config: DurableAgentConfig | None = None,
    ) -> None:
        self.agent_host = agent_host
        self.store = store
        self.coordinator = coordinator
        self.worker = worker
        self._plan_factory = plan_factory
        self.config = config or DurableAgentConfig()
        self._worker_task: asyncio.Task[None] | None = None
        self._started = False
        self._lifecycle_lock = asyncio.Lock()

    @classmethod
    def from_temporal(
        cls,
        agent_host: AgentHost,
        store: PostgresDurableStore,
        client: Client,
        *,
        config: DurableAgentConfig | None = None,
    ) -> DurableAgentProfile:
        settings = config or DurableAgentConfig()
        temporal_coordinator = TemporalCoordinator(client, task_queue=settings.task_queue)
        worker = build_temporal_worker(
            client,
            agent_host.runtime,
            task_queue=settings.task_queue,
        )
        return cls(
            agent_host,
            store,
            temporal_coordinator,
            worker,
            plan_factory=lambda input_value, workflow_id: TemporalExecutionPlan(
                client,
                settings.task_queue,
                input_value,
                workflow_id=workflow_id,
            ),
            config=settings,
        )

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
                await self._wait_worker_ready()
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
        execution = await self.coordinator.submit(self._plan_factory(input, selected_workflow_id))
        handle = DurableExecutionHandle(self, execution, stream_id, request.invocation_id)
        try:
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
        except Exception as exc:
            handle.record_audit_error("started", exc)
        return handle

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
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return
        try:
            async with asyncio.timeout(self.config.shutdown_timeout_seconds):
                await self.worker.shutdown()
                await task
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _wait_worker_ready(self) -> None:
        task = self._worker_task
        if task is None:
            raise RuntimeError("durable worker task was not created")
        async with asyncio.timeout(self.config.worker_start_timeout_seconds):
            while not self.worker.is_running:
                if task.done():
                    await task
                    raise RuntimeError("durable worker stopped before becoming ready")
                await asyncio.sleep(0.01)

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
        self._audit_errors: list[str] = []

    @property
    def workflow_id(self) -> str:
        return self._execution.workflow_id

    @property
    def run_id(self) -> str | None:
        return self._execution.run_id

    @property
    def audit_errors(self) -> tuple[str, ...]:
        return tuple(self._audit_errors)

    def record_audit_error(self, phase: str, error: Exception) -> None:
        self._audit_errors.append(f"{phase}: {error}")

    async def wait(self) -> InvocationResult:
        async with self._wait_lock:
            if self._result is None:
                execution_result = await self._execution.wait()
                self._result = _invocation_result(execution_result, self.invocation_id)
                try:
                    await self._profile.store.append(
                        self._stream_id,
                        "completed",
                        "durable.invocation.completed",
                        _result_payload(self._result),
                    )
                except Exception as exc:
                    self.record_audit_error("completed", exc)
            return self._result

    async def cancel(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("cancellation reason must not be empty")
        try:
            await self._profile.store.append(
                self._stream_id,
                "cancel-requested",
                "durable.invocation.cancel_requested",
                {"invocation_id": self.invocation_id, "reason": reason},
            )
        except Exception as exc:
            self.record_audit_error("cancel-requested", exc)
        await self._execution.cancel(reason)


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


def _invocation_result(result: ExecutionResult, invocation_id: str) -> InvocationResult:
    status = {
        ExecutionStatus.FAILED: InvocationStatus.FAILED,
        ExecutionStatus.CANCELLED: InvocationStatus.CANCELLED,
        ExecutionStatus.RECONCILIATION_REQUIRED: InvocationStatus.RECONCILIATION_REQUIRED,
        ExecutionStatus.SUCCEEDED: InvocationStatus.SUCCEEDED,
    }.get(result.status)
    if status is None:
        raise ValueError(f"unsupported execution result status: {result.status.value}")
    return InvocationResult(
        invocation_id=invocation_id,
        status=status,
        output=result.output,
        error_code=result.error_code,
        error_message=result.error_message,
    )
