from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from misaka_approval_capability import (
    DecisionRecord,
    DecisionStore,
)
from misaka_approval_jsonl import JsonlDecisionStore
from misaka_coordinator_adapters import InvocationExecutionPlan
from misaka_coordinator_runtime import (
    DirectCoordinator,
    DirectExecutionHandle,
    ExecutionResult,
    ExecutionStatus,
)
from misaka_delegation_capability import DelegationGatewayPort, DelegationUnauthorized
from misaka_delegation_contracts import (
    ContinuationRequest,
    DelegationRequest,
    DelegationSnapshot,
)
from misaka_delegation_jsonl import JsonlDelegationStore
from misaka_delegation_runtime import DelegationRuntime, RuntimeDelegationGateway
from misaka_interaction_contracts import (
    DecisionProposal,
    DecisionRef,
    DecisionStatus,
    InteractionMessage,
    InteractionMessageDraft,
    MessageCursor,
    PrincipalKind,
    PrincipalRef,
    ScopeRef,
)
from misaka_interaction_jsonl import JsonlInteractionChannelStore
from misaka_invocation_contracts import (
    CompletionBoundary,
    InvocationRequest,
)
from misaka_invocation_runtime import InvocationRuntime
from misaka_kernel import CompositionSnapshot, ProfileDefinition, ProfileLoader
from misaka_kernel_contracts import JsonObject, ModuleId
from misaka_persistence_contracts import DurableJob, DurableJobStatus
from misaka_persistence_jsonl import JsonlEventLog, JsonlJobRegistry
from misaka_service_runtime import ServiceManager, ServiceSnapshot

from misaka_control_plane.delegation_gateway_policy import (
    DelegationDecisionGate,
    WorkspaceCatalog,
    delegation_request_from_submission,
)
from misaka_control_plane.models import (
    CapabilityView,
    DecisionSubmission,
    DelegationApprovalSubmission,
    DelegationSubmission,
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


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    profile_id: str = "control-plane"
    profile_version: str = "1.0.0"
    transport_ids: tuple[str, ...] = ("fastapi", "in-process")
    fact_owners: tuple[tuple[str, str], ...] = (
        ("job.lifecycle", "persistence.job.jsonl"),
        ("template.definition", "persistence.template.jsonl"),
        ("trigger.definition", "persistence.trigger.jsonl"),
        ("decision.fact", "persistence.decision.jsonl"),
        ("delegation.lifecycle", "persistence.delegation.jsonl"),
        ("interaction.message", "persistence.interaction.jsonl"),
        ("invocation.execution", "runtime.invocation"),
        ("managed-service.lifecycle", "runtime.managed-service"),
    )
    projection_sources: tuple[tuple[str, str], ...] = (
        ("job.projection", "job.lifecycle"),
        ("template.projection", "template.definition"),
        ("trigger.projection", "trigger.definition"),
        ("decision.projection", "decision.fact"),
        ("delegation.snapshot", "delegation.lifecycle"),
        ("interaction.channel", "interaction.message"),
        ("invocation.snapshot", "invocation.execution"),
        ("service.snapshot", "managed-service.lifecycle"),
    )
    projection_watermark_owners: tuple[tuple[str, str], ...] = (
        ("job.projection", "persistence.job.jsonl"),
        ("template.projection", "persistence.template.jsonl"),
        ("trigger.projection", "persistence.trigger.jsonl"),
        ("decision.projection", "persistence.decision.jsonl"),
        ("delegation.snapshot", "persistence.delegation.jsonl"),
        ("interaction.channel", "persistence.interaction.jsonl"),
        ("invocation.snapshot", "runtime.invocation"),
        ("service.snapshot", "runtime.managed-service"),
    )
    resource_owners: tuple[tuple[str, str], ...] = (
        ("fastapi", "transport.fastapi"),
        ("event-log", "persistence.jsonl"),
        ("coordinator", "runtime.coordinator"),
        ("delegation-channel", "runtime.delegation"),
        ("delegation-gateway", "gateway.delegation"),
        ("managed-service", "runtime.managed-service"),
    )

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.profile_version.strip():
            raise ValueError("control-plane profile identity must not be empty")
        if any(not item.strip() for item in self.transport_ids):
            raise ValueError("control-plane transport ids must not be empty")
        if len(self.transport_ids) != len(set(self.transport_ids)):
            raise ValueError("control-plane transport ids must be unique")
        for label, values in (
            ("fact owners", self.fact_owners),
            ("projection sources", self.projection_sources),
            ("projection watermark owners", self.projection_watermark_owners),
            ("resource owners", self.resource_owners),
        ):
            _validate_metadata_pairs(values, label)


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
        decision_store: DecisionStore | None = None,
        delegation_gateway: DelegationGatewayPort | None = None,
        workspace_catalog: WorkspaceCatalog | None = None,
        service_manager: ServiceManager | None = None,
        config: ControlPlaneConfig | None = None,
    ) -> None:
        self._runtime = runtime
        self._coordinator = DirectCoordinator(
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )
        self._log = JsonlEventLog(state_path)
        self._registry = JsonlJobRegistry(self._log)
        self._template_registry = JsonlTemplateRegistry(self._log)
        self._trigger_registry = JsonlTriggerRegistry(self._log)
        self._decision_store = decision_store or JsonlDecisionStore(self._log)
        self._delegation_decision_gate = DelegationDecisionGate(self._decision_store)
        self._workspace_catalog = workspace_catalog or WorkspaceCatalog()
        self._delegation_store: JsonlDelegationStore | None = None
        self._interaction_store: JsonlInteractionChannelStore | None = None
        self._delegation_runtime: DelegationRuntime | None = None
        if delegation_gateway is None:
            self._delegation_store = JsonlDelegationStore(self._log)
            self._interaction_store = JsonlInteractionChannelStore(self._log)
            self._delegation_runtime = DelegationRuntime(
                runtime,
                self._interaction_store,
                store=self._delegation_store,
                gate=self._delegation_decision_gate,
            )
            delegation_gateway = RuntimeDelegationGateway(
                self._delegation_runtime,
                self._interaction_store,
            )
        self._delegation_gateway = delegation_gateway
        self._service_manager = service_manager or ServiceManager(())
        self._provider_setup = provider_setup
        self._dag_runner = dag_runner
        self.config = config or ControlPlaneConfig()
        self._composition_snapshot = _composition_snapshot(
            self.config,
            workflow_enabled=dag_runner is not None,
        )
        self._handles: dict[str, DirectExecutionHandle] = {}
        self._instance_handles: dict[str, DirectExecutionHandle] = {}
        self._instance_tasks: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._started

    @property
    def composition_snapshot(self) -> CompositionSnapshot:
        return self._composition_snapshot

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            if self._provider_setup is not None:
                await self._provider_setup(self._runtime)
            await self._registry.open()
            await self._template_registry.open()
            await self._trigger_registry.open()
            await self._decision_store.list()
            if self._delegation_store is not None:
                await self._delegation_store.open()
            if self._interaction_store is not None:
                await self._interaction_store.open()
            await self._service_manager.start()
            await self._coordinator.start()
            self._started = True
        try:
            await self._recover_jobs()
            await self._recover_delegations()
        except BaseException:
            await self.stop()
            raise

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
        if self._delegation_runtime is not None:
            await self._delegation_runtime.stop()
        await self._log.close()
        await self._service_manager.close()

    async def services(self) -> tuple[ServiceSnapshot, ...]:
        self._require_started()
        return await self._service_manager.list()

    async def service(self, service_id: str) -> ServiceSnapshot:
        self._require_started()
        return await self._service_manager.get(service_id)

    async def start_service(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot:
        self._require_started()
        return await self._service_manager.start_service(service_id, expected_epoch=expected_epoch)

    async def stop_service(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot:
        self._require_started()
        return await self._service_manager.stop(service_id, expected_epoch=expected_epoch)

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

    async def create_delegation(
        self, request: DelegationRequest, actor: PrincipalRef
    ) -> DelegationSnapshot:
        self._require_started()
        if (actor.principal_id, actor.kind) != (
            request.initiator.principal_id,
            request.initiator.kind,
        ):
            raise DelegationUnauthorized(
                "delegation.initiator_forbidden",
                "delegation creator must match the declared initiator",
            )
        await self._delegation_decision_gate.authorize(request, None)
        return await self._delegation_gateway.create(request, actor)

    async def submit_delegation(self, submission: DelegationSubmission) -> DelegationSnapshot:
        request = delegation_request_from_submission(
            submission,
            self._workspace_catalog,
        )
        actor = PrincipalRef(
            submission.actor.principal_id,
            submission.actor.kind,
            submission.actor.display_name,
        )
        return await self.create_delegation(request, actor)

    async def delegation(self, delegation_id: str, actor: PrincipalRef) -> DelegationSnapshot:
        self._require_started()
        return await self._delegation_gateway.get(delegation_id, actor)

    async def delegation_children(
        self, delegation_id: str, actor: PrincipalRef
    ) -> tuple[DelegationSnapshot, ...]:
        self._require_started()
        return await self._delegation_gateway.children(delegation_id, actor)

    async def send_delegation_message(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        draft: InteractionMessageDraft,
    ) -> InteractionMessage:
        self._require_started()
        return await self._delegation_gateway.send(delegation_id, actor, draft)

    async def delegation_events(
        self,
        delegation_id: str,
        actor: PrincipalRef,
        *,
        cursor: MessageCursor | None = None,
    ) -> tuple[InteractionMessage, ...]:
        self._require_started()
        return await self._delegation_gateway.events(
            delegation_id,
            actor,
            cursor=cursor,
        )

    async def reply_delegation(self, request: ContinuationRequest) -> DelegationSnapshot:
        self._require_started()
        return await self._delegation_gateway.reply(request)

    async def cancel_delegation(self, request: ContinuationRequest) -> DelegationSnapshot:
        self._require_started()
        return await self._delegation_gateway.cancel(request)

    async def reconcile_delegation(self, request: ContinuationRequest) -> DelegationSnapshot:
        self._require_started()
        return await self._delegation_gateway.reconcile(request)

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

    async def decisions(self) -> tuple[DecisionRecord, ...]:
        self._require_started()
        return await self._decision_store.list()

    async def decision(self, proposal_id: str, revision: int) -> DecisionRecord:
        self._require_started()
        return await self._decision_store.get(DecisionRef(proposal_id, revision))

    async def decide(
        self,
        proposal_id: str,
        revision: int,
        decision: DecisionSubmission,
    ) -> DecisionRecord:
        self._require_started()
        ref = DecisionRef(proposal_id, revision)
        pending = await self._decision_store.get(ref)
        instance_id = _proposal_instance_id(pending.proposal)
        instance = await self._template_registry.get_instance(instance_id)
        record = await self._decision_store.decide(
            ref,
            status=DecisionStatus(decision.decision),
            decided_by=PrincipalRef(decision.principal_id, PrincipalKind.HUMAN),
            reason=decision.reason,
        )
        if decision.decision == DecisionStatus.APPROVED.value:
            if (
                instance.status
                in {
                    DurableJobStatus.QUEUED,
                    DurableJobStatus.WAITING_DECISION,
                }
                and instance.instance_id not in self._instance_tasks
            ):
                self._schedule_instance(instance.instance_id)
            return record
        if instance.status is DurableJobStatus.WAITING_DECISION:
            await self._template_registry.transition_instance(
                instance.instance_id,
                DurableJobStatus.FAILED,
                expected_version=instance.version,
                error_code="control.decision_rejected",
                error_message=decision.reason or "decision was rejected",
            )
        elif instance.status is DurableJobStatus.RUNNING:
            await self._template_registry.transition_instance(
                instance.instance_id,
                DurableJobStatus.RECONCILIATION_REQUIRED,
                expected_version=instance.version,
                error_code="control.decision_rejected_after_start",
                error_message=decision.reason or "decision was rejected after execution started",
            )
        return record

    async def approve_delegation(
        self,
        delegation_id: str,
        approval: DelegationApprovalSubmission,
    ) -> DecisionRecord:
        self._require_started()
        if approval.actor.kind is not PrincipalKind.HUMAN:
            raise DelegationUnauthorized(
                "delegation.approver_forbidden",
                "delegation approval requires a human principal",
            )
        ref = DecisionRef(
            approval.decision_ref.proposal_id,
            approval.decision_ref.revision,
        )
        pending = await self._decision_store.get(ref)
        proposal_delegation_id = _proposal_delegation_id(pending.proposal)
        if proposal_delegation_id != delegation_id:
            raise ValueError("delegation approval path does not match the Decision proposal")
        if pending.proposal.plan_hash != approval.plan_hash:
            raise ValueError("delegation approval plan hash is stale")
        return await self._decision_store.decide(
            ref,
            status=DecisionStatus.APPROVED,
            decided_by=PrincipalRef(
                approval.actor.principal_id,
                approval.actor.kind,
                approval.actor.display_name,
            ),
            reason=approval.reason,
        )

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
            handle = await self._coordinator.submit(
                InvocationExecutionPlan(
                    self._runtime,
                    request,
                    provider_id=submission.provider_id,
                )
            )
            self._handles[job_id] = handle
            result = await handle.wait()
            status = _status_from_execution(result.status)
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

    async def _recover_delegations(self) -> None:
        if self._delegation_runtime is not None:
            await self._delegation_runtime.recover()

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
                DurableJobStatus.WAITING_DECISION,
            }:
                if template.definition.decision_required:
                    proposal = _instance_decision_proposal(instance, template)
                    decision = await self._decision_store.ensure(proposal)
                    if decision.fact is None:
                        if instance.status is DurableJobStatus.QUEUED:
                            await self._template_registry.transition_instance(
                                instance_id,
                                DurableJobStatus.WAITING_DECISION,
                                expected_version=instance.version,
                            )
                        return
                    if decision.fact.status is not DecisionStatus.APPROVED:
                        if instance.status is not DurableJobStatus.FAILED:
                            await self._template_registry.transition_instance(
                                instance_id,
                                DurableJobStatus.FAILED,
                                expected_version=instance.version,
                                error_code="control.decision_rejected",
                                error_message=(
                                    decision.fact.reason
                                    or f"decision was {decision.fact.status.value}"
                                ),
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
                status = _status_from_execution(result.status)
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
    ) -> ExecutionResult:
        node = template.definition.nodes[0]
        request = _node_request(instance, node.node_id, node)
        handle = await self._coordinator.submit(
            InvocationExecutionPlan(
                self._runtime,
                request,
                provider_id=node.provider_id,
            )
        )
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


def _instance_decision_proposal(
    instance: InstanceRecord,
    template: TemplateRecord,
) -> DecisionProposal:
    plan_payload = {
        "template_id": template.definition.template_id,
        "template_version": template.definition.version,
        "coordinator": template.definition.coordinator,
        "nodes": [node.model_dump(mode="json") for node in template.definition.nodes],
        "instance_input": instance.input,
    }
    canonical = json.dumps(
        plan_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    effects = tuple(
        dict.fromkeys(
            f"{node.capability_id}:{node.operation}" for node in template.definition.nodes
        )
    )
    return DecisionProposal(
        ref=DecisionRef(
            proposal_id=f"instance:{instance.instance_id}",
            revision=1,
        ),
        plan_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        requested_effects=effects,
        scope=ScopeRef(f"instance:{instance.instance_id}"),
        created_by=PrincipalRef("control-plane", PrincipalKind.APPLICATION),
        payload={
            "instance_id": instance.instance_id,
            "template_id": template.definition.template_id,
            "template_version": template.definition.version,
        },
        policy_snapshot={"decision_required": True},
    )


def _proposal_instance_id(proposal: DecisionProposal) -> str:
    instance_id = proposal.payload.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise RuntimeError("decision proposal is not bound to a Control Plane instance")
    return instance_id


def _proposal_delegation_id(proposal: DecisionProposal) -> str:
    delegation_id = proposal.payload.get("delegation_id")
    if not isinstance(delegation_id, str) or not delegation_id.strip():
        raise ValueError("decision proposal is not bound to a Delegation")
    return delegation_id


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
        policy_context={"network_policy": submission.network_policy},
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
        policy_context={"network_policy": node.network_policy},
    )


def _invocation_payload(result: ExecutionResult) -> JsonObject:
    payload: JsonObject = {"status": result.status.value}
    if result.output is not None:
        payload["output"] = result.output
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    if result.error_message is not None:
        payload["error_message"] = result.error_message
    return payload


def _status_from_execution(status: ExecutionStatus) -> DurableJobStatus:
    return {
        ExecutionStatus.SUCCEEDED: DurableJobStatus.SUCCEEDED,
        ExecutionStatus.FAILED: DurableJobStatus.FAILED,
        ExecutionStatus.CANCELLED: DurableJobStatus.CANCELLED,
        ExecutionStatus.RECONCILIATION_REQUIRED: DurableJobStatus.RECONCILIATION_REQUIRED,
    }[status]


_TERMINAL_STATUSES = frozenset(
    {
        DurableJobStatus.SUCCEEDED,
        DurableJobStatus.FAILED,
        DurableJobStatus.CANCELLED,
        DurableJobStatus.RECONCILIATION_REQUIRED,
    }
)


class ControlPlaneProfile(ControlPlaneService):
    """Explicit application-profile name for the Control Plane service."""


def _composition_snapshot(
    config: ControlPlaneConfig,
    *,
    workflow_enabled: bool,
) -> CompositionSnapshot:
    module_ids = [
        "runtime.invocation",
        "runtime.coordinator",
        "persistence.job.jsonl",
        "persistence.template.jsonl",
        "persistence.trigger.jsonl",
        "persistence.decision.jsonl",
        "persistence.delegation.jsonl",
        "persistence.interaction.jsonl",
        "runtime.delegation",
        "gateway.delegation",
        "runtime.managed-service",
        "transport.fastapi",
    ]
    if workflow_enabled:
        module_ids.append("profile.control-plane-workflow")
    configurations: dict[ModuleId, JsonObject] = {
        ModuleId("transport.fastapi"): {"enabled": True},
    }
    if workflow_enabled:
        configurations[ModuleId("profile.control-plane-workflow")] = {"enabled": True}
    definition = ProfileDefinition(
        profile_id=config.profile_id,
        profile_version=config.profile_version,
        module_ids=tuple(ModuleId(module_id) for module_id in module_ids),
        configurations=configurations,
        transport_ids=config.transport_ids,
        fact_owners=dict(config.fact_owners),
        projection_sources=dict(config.projection_sources),
        projection_watermark_owners=dict(config.projection_watermark_owners),
        resource_owners=dict(config.resource_owners),
    )
    return ProfileLoader({}).snapshot(definition)


def _validate_metadata_pairs(values: tuple[tuple[str, str], ...], label: str) -> None:
    keys = tuple(key for key, _ in values)
    if len(values) != len(set(values)) or len(keys) != len(set(keys)):
        raise ValueError(f"control-plane {label} must be unique")
    if any(not key.strip() or not value.strip() for key, value in values):
        raise ValueError(f"control-plane {label} keys and values must not be empty")
