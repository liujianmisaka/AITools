from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from misaka_coordinator_runtime import DirectCoordinator, DirectExecutionHandle
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationRequest,
    InvocationStatus,
)
from misaka_invocation_runtime import InvocationRuntime
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableJob, DurableJobStatus
from misaka_persistence_jsonl import JsonlEventLog, JsonlJobRegistry

from misaka_control_plane.models import CapabilityView, JobSubmission, ModelCatalogView, ModelView


class ControlPlaneService:
    """Local control-plane orchestration; provider discovery stays in InvocationRuntime."""

    def __init__(
        self,
        runtime: InvocationRuntime,
        *,
        state_path: str | Path,
        shutdown_timeout_seconds: float = 15.0,
        provider_setup: Callable[[InvocationRuntime], Awaitable[None]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._coordinator = DirectCoordinator(
            runtime,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )
        self._log = JsonlEventLog(state_path)
        self._registry = JsonlJobRegistry(self._log)
        self._provider_setup = provider_setup
        self._handles: dict[str, DirectExecutionHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            if self._provider_setup is not None:
                await self._provider_setup(self._runtime)
            await self._registry.open()
            await self._coordinator.start()
            self._started = True
        await self._recover_jobs()

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            self._started = False
        await self._coordinator.stop()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        await self._log.close()

    async def submit(self, submission: JobSubmission) -> DurableJob:
        self._require_started()
        request = _invocation_request(submission)
        job, created = await self._registry.register(
            submission.job_id,
            submission.idempotency_key,
            _request_payload(submission),
        )
        if not created:
            return job
        self._schedule(submission, request)
        return job

    async def get(self, job_id: str) -> DurableJob:
        self._require_started()
        return await self._registry.get(job_id)

    async def list(self) -> tuple[DurableJob, ...]:
        self._require_started()
        return await self._registry.list()

    def capabilities(self) -> list[CapabilityView]:
        self._require_started()
        return [
            CapabilityView(
                capability_id=descriptor.capability_id,
                version=descriptor.version,
                operations=[operation.name for operation in descriptor.operations],
                features=sorted(feature.value for feature in descriptor.features),
            )
            for descriptor in self._runtime.descriptors()
        ]

    async def models(self) -> list[ModelCatalogView]:
        self._require_started()
        catalogs = await self._runtime.model_catalogs()
        return [
            ModelCatalogView(
                provider_id=catalog.provider_id,
                models=[
                    ModelView(
                        model_id=model.model_id,
                        display_name=model.display_name,
                        description=model.description,
                        supported_efforts=list(model.supported_efforts),
                    )
                    for model in catalog.models
                ],
            )
            for catalog in catalogs
        ]

    async def cancel(self, job_id: str, reason: str) -> DurableJob:
        self._require_started()
        handle = self._handles.get(job_id)
        if handle is not None:
            await handle.cancel(reason)
            return await self._registry.get(job_id)
        job = await self._registry.get(job_id)
        if job.status in _TERMINAL_STATUSES:
            return job
        return await self._registry.transition(
            job_id,
            DurableJobStatus.CANCELLED,
            expected_version=job.version,
            error_code="control.cancelled",
            error_message=reason,
        )

    async def _drive(self, submission: JobSubmission, request: InvocationRequest) -> None:
        job_id = submission.job_id
        try:
            current = await self._registry.get(job_id)
            if current.status is DurableJobStatus.QUEUED:
                current = await self._registry.transition(
                    job_id,
                    DurableJobStatus.RUNNING,
                    expected_version=current.version,
                )
            elif current.status is not DurableJobStatus.RUNNING:
                return
            handle = await self._coordinator.submit(request, provider_id=submission.provider_id)
            self._handles[job_id] = handle
            result = await handle.wait()
            status = _status_from_invocation(result.status)
            current = await self._registry.get(job_id)
            await self._registry.transition(
                job_id,
                status,
                expected_version=current.version,
                result=(result.output if isinstance(result.output, dict) else None),
                error_code=result.error_code,
                error_message=result.error_message,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                current = await self._registry.get(job_id)
                if current.status not in _TERMINAL_STATUSES:
                    await self._registry.transition(
                        job_id,
                        DurableJobStatus.RECONCILIATION_REQUIRED,
                        expected_version=current.version,
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=str(exc),
                    )
            except Exception:
                pass
        finally:
            self._handles.pop(job_id, None)

    def _schedule(self, submission: JobSubmission, request: InvocationRequest) -> None:
        task = asyncio.create_task(self._drive(submission, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _recover_jobs(self) -> None:
        """Resume only pre-start jobs; fence jobs whose external state is unknown."""
        for job in await self._registry.list():
            if job.status is DurableJobStatus.RUNNING:
                try:
                    await self._registry.transition(
                        job.job_id,
                        DurableJobStatus.RECONCILIATION_REQUIRED,
                        expected_version=job.version,
                        error_code="control.restart_reconciliation_required",
                        error_message=(
                            "The service restarted while this job was running; "
                            "the external invocation cannot be proven safe to resume."
                        ),
                    )
                except Exception:
                    continue
            elif job.status is DurableJobStatus.QUEUED:
                try:
                    submission = JobSubmission.model_validate(job.request)
                    self._schedule(submission, _invocation_request(submission))
                except Exception as exc:
                    try:
                        current = await self._registry.get(job.job_id)
                        await self._registry.transition(
                            job.job_id,
                            DurableJobStatus.RECONCILIATION_REQUIRED,
                            expected_version=current.version,
                            error_code="control.recovery_request_invalid",
                            error_message=str(exc),
                        )
                    except Exception:
                        continue

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("control plane service is not started")


def _request_payload(submission: JobSubmission) -> JsonObject:
    return submission.model_dump(mode="json")


def _invocation_request(submission: JobSubmission) -> InvocationRequest:
    return InvocationRequest(
        invocation_id=f"control:{submission.job_id}",
        capability_id=submission.capability_id,
        operation=submission.operation,
        input=submission.input,
        idempotency_key=submission.idempotency_key,
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        output_schema=submission.output_schema,
        model=submission.model,
        effort=submission.effort,
    )


def _status_from_invocation(status: InvocationStatus) -> DurableJobStatus:
    return {
        InvocationStatus.SUCCEEDED: DurableJobStatus.SUCCEEDED,
        InvocationStatus.FAILED: DurableJobStatus.FAILED,
        InvocationStatus.REJECTED: DurableJobStatus.FAILED,
        InvocationStatus.CANCELLED: DurableJobStatus.CANCELLED,
        InvocationStatus.RECONCILIATION_REQUIRED: DurableJobStatus.RECONCILIATION_REQUIRED,
    }[status]


_TERMINAL_STATUSES = frozenset(
    {
        DurableJobStatus.SUCCEEDED,
        DurableJobStatus.FAILED,
        DurableJobStatus.CANCELLED,
        DurableJobStatus.RECONCILIATION_REQUIRED,
    }
)
