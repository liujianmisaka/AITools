from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from misaka_approval_capability import (
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStore,
)
from misaka_approval_jsonl import JsonlApprovalStore
from misaka_coordinator_runtime import DirectCoordinator, DirectExecutionHandle
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
)
from misaka_invocation_runtime import InvocationRuntime
from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableJob, DurableJobStatus
from misaka_persistence_jsonl import JsonlEventLog, JsonlJobRegistry
from misaka_service_runtime import ServiceManager, ServiceSnapshot

from misaka_control_plane.models import (
    ApprovalDecisionSubmission,
    CapabilityView,
    EventSubmission,
    InstanceSubmission,
    JobSubmission,
    ModelCatalogView,
    ModelView,
    TemplateNodeSubmission,
    TemplateSubmission,
    TriggerSubmission,
)
from misaka_control_plane.template_registry import (
    InstanceRecord,
    JsonlTemplateRegistry,
    TemplateRecord,
)
from misaka_control_plane.trigger_registry import JsonlTriggerRegistry, TriggerRecord


@dataclass(frozen=True, slots=True)
class TemplateRunResult:
    status: DurableJobStatus
    result: JsonObject = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


TemplateDAGRunner = Callable[[InstanceRecord, TemplateRecord], Awaitable[TemplateRunResult]]


class ControlPlaneService:
    """Local control-plane orchestration; provider discovery stays in InvocationRuntime."""

    def __init__(
        self,
        runtime: InvocationRuntime,
        *,
        state_path: str | Path,
        shutdown_timeout_seconds: float = 15.0,
        provider_setup: Callable[[InvocationRuntime], Awaitable[None]] | None = None,
        dag_runner: TemplateDAGRunner | None = None,
        approval_store: ApprovalStore | None = None,
        service_manager: ServiceManager | None = None,
    ) -> None:
        self._runtime = runtime
        self._coordinator = DirectCoordinator(
            runtime,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )
        self._log = JsonlEventLog(state_path)
        self._registry = JsonlJobRegistry(self._log)
        self._template_registry = JsonlTemplateRegistry(self._log)
        self._trigger_registry = JsonlTriggerRegistry(self._log)
        self._approval_store = approval_store or JsonlApprovalStore(self._log)
        self._service_manager = service_manager or ServiceManager(())
        self._provider_setup = provider_setup
        self._dag_runner = dag_runner
        self._handles: dict[str, DirectExecutionHandle] = {}
        self._instance_handles: dict[str, DirectExecutionHandle] = {}
        self._instance_tasks: dict[str, asyncio.Task[None]] = {}
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
            await self._template_registry.open()
            await self._trigger_registry.open()
            await self._approval_store.list()
            await self._service_manager.start()
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
        if self._instance_tasks:
            await asyncio.gather(*tuple(self._instance_tasks.values()), return_exceptions=True)
        await self._log.close()
        await self._service_manager.close()

    async def services(self) -> tuple[ServiceSnapshot, ...]:
        self._require_started()
        return await self._service_manager.list()

    async def service(self, service_id: str) -> ServiceSnapshot:
        self._require_started()
        return await self._service_manager.get(service_id)

    async def start_service(self, service_id: str) -> ServiceSnapshot:
        self._require_started()
        return await self._service_manager.start_service(service_id)

    async def stop_service(self, service_id: str) -> ServiceSnapshot:
        self._require_started()
        return await self._service_manager.stop(service_id)

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

    async def create_template(self, definition: TemplateSubmission) -> TemplateRecord:
        self._require_started()
        return await self._template_registry.create_template(definition)

    async def templates(self) -> tuple[TemplateRecord, ...]:
        self._require_started()
        return await self._template_registry.list_templates()

    async def template(self, template_id: str, version: int | None = None) -> TemplateRecord:
        self._require_started()
        return await self._template_registry.get_template(template_id, version)

    async def start_instance(
        self,
        template_id: str,
        template_version: int | None,
        submission: InstanceSubmission,
    ) -> InstanceRecord:
        self._require_started()
        template = await self._template_registry.get_template(template_id, template_version)
        instance, created = await self._template_registry.create_instance(
            submission.instance_id,
            submission.idempotency_key,
            template.definition.template_id,
            template.definition.version,
            submission.input,
        )
        if created:
            self._schedule_instance(instance.instance_id)
        return instance

    async def get_instance(self, instance_id: str) -> InstanceRecord:
        self._require_started()
        return await self._template_registry.get_instance(instance_id)

    async def instances(self) -> tuple[InstanceRecord, ...]:
        self._require_started()
        return await self._template_registry.list_instances()

    async def create_trigger(self, definition: TriggerSubmission) -> TriggerRecord:
        self._require_started()
        await self._template_registry.get_template(
            definition.template_id, definition.template_version
        )
        return await self._trigger_registry.register(definition)

    async def triggers(self) -> tuple[TriggerRecord, ...]:
        self._require_started()
        return await self._trigger_registry.list()

    async def publish_event(self, event: EventSubmission) -> tuple[str, ...]:
        self._require_started()
        instance_ids: list[str] = []
        for trigger in await self._trigger_registry.matching(event.event_type):
            instance_id = f"trigger:{trigger.definition.trigger_id}:{event.event_id}"
            instance, created = await self._template_registry.create_instance(
                instance_id,
                instance_id,
                trigger.definition.template_id,
                trigger.definition.template_version,
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "data": event.data,
                },
            )
            if created:
                self._schedule_instance(instance.instance_id)
            await self._trigger_registry.record_delivery(
                trigger.definition.trigger_id,
                event.event_id,
                instance.instance_id,
            )
            instance_ids.append(instance.instance_id)
        return tuple(instance_ids)

    async def approvals(self) -> tuple[ApprovalRecord, ...]:
        self._require_started()
        return await self._approval_store.list()

    async def approval(self, approval_id: str) -> ApprovalRecord:
        self._require_started()
        return await self._approval_store.get(approval_id)

    async def decide_approval(
        self,
        approval_id: str,
        decision: ApprovalDecisionSubmission,
    ) -> ApprovalRecord:
        self._require_started()
        approval = await self._approval_store.decide(
            approval_id,
            ApprovalDecision(
                ApprovalDecisionValue(decision.decision),
                reason=decision.reason,
            ),
        )
        instance = await self._template_registry.get_instance(approval.request.instance_id)
        if decision.decision == "approve":
            if (
                instance.status
                in {
                    DurableJobStatus.QUEUED,
                    DurableJobStatus.WAITING_APPROVAL,
                }
                and instance.instance_id not in self._instance_tasks
            ):
                self._schedule_instance(instance.instance_id)
            return approval
        if instance.status is DurableJobStatus.WAITING_APPROVAL:
            await self._template_registry.transition_instance(
                instance.instance_id,
                DurableJobStatus.FAILED,
                expected_version=instance.version,
                error_code="control.approval_rejected",
                error_message=decision.reason or "approval was rejected",
            )
        elif instance.status is DurableJobStatus.RUNNING:
            await self._template_registry.transition_instance(
                instance.instance_id,
                DurableJobStatus.RECONCILIATION_REQUIRED,
                expected_version=instance.version,
                error_code="control.approval_rejected_after_start",
                error_message=decision.reason or "approval was rejected after execution started",
            )
        return approval

    async def cancel_instance(self, instance_id: str, reason: str) -> InstanceRecord:
        self._require_started()
        handle = self._instance_handles.get(instance_id)
        if handle is not None:
            await handle.cancel(reason)
            return await self._template_registry.get_instance(instance_id)
        instance = await self._template_registry.get_instance(instance_id)
        if instance.status in _TERMINAL_STATUSES:
            return instance
        if instance_id in self._instance_tasks:
            return await self._template_registry.transition_instance(
                instance_id,
                DurableJobStatus.RECONCILIATION_REQUIRED,
                expected_version=instance.version,
                error_code="control.instance_cancel_unknown",
                error_message=reason,
            )
        return await self._template_registry.transition_instance(
            instance_id,
            DurableJobStatus.CANCELLED,
            expected_version=instance.version,
            error_code="control.instance_cancelled",
            error_message=reason,
        )

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
        for instance in await self._template_registry.list_instances():
            if instance.status is DurableJobStatus.RUNNING:
                try:
                    await self._template_registry.transition_instance(
                        instance.instance_id,
                        DurableJobStatus.RECONCILIATION_REQUIRED,
                        expected_version=instance.version,
                        error_code="control.restart_reconciliation_required",
                        error_message=(
                            "The service restarted while this instance was running; "
                            "the external invocation cannot be proven safe to resume."
                        ),
                    )
                except Exception:
                    continue
            elif instance.status is DurableJobStatus.QUEUED:
                self._schedule_instance(instance.instance_id)

    def _schedule_instance(self, instance_id: str) -> None:
        task = asyncio.create_task(self._drive_instance(instance_id))
        self._instance_tasks[instance_id] = task
        task.add_done_callback(lambda _, key=instance_id: self._instance_tasks.pop(key, None))

    async def _drive_instance(self, instance_id: str) -> None:
        try:
            instance = await self._template_registry.get_instance(instance_id)
            template = await self._template_registry.get_template(
                instance.template_id, instance.template_version
            )
            if instance.status in {
                DurableJobStatus.QUEUED,
                DurableJobStatus.WAITING_APPROVAL,
            }:
                if template.definition.approval_required:
                    approval = await self._approval_store.ensure(
                        ApprovalRequest(approval_id=instance_id, instance_id=instance_id)
                    )
                    if approval.decision is None:
                        if instance.status is DurableJobStatus.QUEUED:
                            await self._template_registry.transition_instance(
                                instance_id,
                                DurableJobStatus.WAITING_APPROVAL,
                                expected_version=instance.version,
                            )
                        return
                    if approval.decision.value is ApprovalDecisionValue.REJECT:
                        if instance.status is not DurableJobStatus.FAILED:
                            await self._template_registry.transition_instance(
                                instance_id,
                                DurableJobStatus.FAILED,
                                expected_version=instance.version,
                                error_code="control.approval_rejected",
                                error_message=approval.decision.reason or "approval was rejected",
                            )
                        return
                instance = await self._template_registry.transition_instance(
                    instance_id,
                    DurableJobStatus.RUNNING,
                    expected_version=instance.version,
                )
            elif instance.status is not DurableJobStatus.RUNNING:
                return
            if template.definition.coordinator == "direct":
                result = await self._run_direct_instance(instance, template)
                status = _status_from_invocation(result.status)
                payload = _invocation_payload(result)
                await self._template_registry.transition_instance(
                    instance_id,
                    status,
                    result=payload if status is DurableJobStatus.SUCCEEDED else None,
                    error_code=result.error_code,
                    error_message=result.error_message,
                )
                return
            dag_result = await self._run_dag_instance(instance, template)
            await self._template_registry.transition_instance(
                instance_id,
                dag_result.status,
                result=(
                    dag_result.result if dag_result.status is DurableJobStatus.SUCCEEDED else None
                ),
                error_code=dag_result.error_code,
                error_message=dag_result.error_message,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                current = await self._template_registry.get_instance(instance_id)
                if current.status not in _TERMINAL_STATUSES:
                    await self._template_registry.transition_instance(
                        instance_id,
                        DurableJobStatus.RECONCILIATION_REQUIRED,
                        expected_version=current.version,
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=str(exc),
                    )
            except Exception:
                pass
        finally:
            self._instance_handles.pop(instance_id, None)

    async def _run_direct_instance(
        self, instance: InstanceRecord, template: TemplateRecord
    ) -> InvocationResult:
        node = template.definition.nodes[0]
        request = _node_request(instance, node.node_id, node)
        handle = await self._coordinator.submit(request, provider_id=node.provider_id)
        self._instance_handles[instance.instance_id] = handle
        return await handle.wait()

    async def _run_dag_instance(
        self, instance: InstanceRecord, template: TemplateRecord
    ) -> TemplateRunResult:
        if self._dag_runner is None:
            raise RuntimeError("no DAG coordinator is attached to this Control Plane profile")
        return await self._dag_runner(instance, template)

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
        policy_context={"network": submission.network_policy},
    )


def _node_request(
    instance: InstanceRecord,
    node_id: str,
    node: TemplateNodeSubmission,
    *,
    extra_input: JsonObject | None = None,
) -> InvocationRequest:
    payload = dict(node.input)
    if instance.input:
        payload.setdefault("instance_input", instance.input)
    if extra_input:
        payload.update(extra_input)
    return InvocationRequest(
        invocation_id=f"instance:{instance.instance_id}:{node_id}",
        capability_id=node.capability_id,
        operation=node.operation,
        input=payload,
        idempotency_key=f"instance:{instance.instance_id}:{node_id}",
        completion_boundary=CompletionBoundary.OPERATION_TERMINAL,
        output_schema=node.output_schema,
        model=node.model,
        effort=node.effort,
        policy_context={"network": node.network_policy},
    )


def _invocation_payload(result: InvocationResult) -> JsonObject:
    payload: JsonObject = {"status": result.status.value}
    if result.output is not None:
        payload["output"] = result.output
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    if result.error_message is not None:
        payload["error_message"] = result.error_message
    return payload


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
