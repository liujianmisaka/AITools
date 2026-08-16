from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from multi_agent.domain.models import (
    EventKind,
    OrchestrationKind,
    ProviderEvent,
    TriggerEventInput,
    WorkflowDefinition,
    WorkflowInstanceStatus,
)
from multi_agent.orchestration.contracts import OrchestrationRuntimeContext
from multi_agent.orchestration.dag import DagOrchestrationModel
from multi_agent.orchestration.execution import AgentWorkExecutor
from multi_agent.orchestration.registry import OrchestrationModelRegistry
from multi_agent.providers.registry import ProviderRegistry
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.workspaces.manager import WorkspaceManager


class WorkflowEventHooks(Protocol):
    async def workflow_instance_created(self, instance_id: str) -> None: ...

    async def workflow_instance_status_changed(
        self,
        instance_id: str,
        *,
        old_status: str,
        new_status: str,
        revision: int,
        error: str | None,
    ) -> None: ...

    async def approval_updated(self, approval_id: str) -> None: ...


class WorkflowEngine:
    """Hosts orchestration runtimes and the isolated agent execution kernel."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        providers: ProviderRegistry,
        workspaces: WorkspaceManager,
        max_concurrency: int = 8,
        provider_concurrency: dict[str, int] | None = None,
        models: OrchestrationModelRegistry | None = None,
        event_hooks: WorkflowEventHooks | None = None,
    ) -> None:
        self.store = store
        self.providers = providers
        self.workspaces = workspaces
        self.models = models or OrchestrationModelRegistry(
            [DagOrchestrationModel()]
        )
        self.executor = AgentWorkExecutor(
            store=store,
            providers=providers,
            workspaces=workspaces,
            max_concurrency=max_concurrency,
            provider_concurrency=provider_concurrency,
        )
        self.event_hooks = event_hooks
        self._instance_tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False
        self._closing = False

    async def start(self) -> dict[str, int]:
        if self._started:
            return {"instances": 0, "work_items": 0, "attempts": 0}
        self.store.initialize()
        recovery_approval_ids = (
            self.store.list_pending_approval_ids_for_recovery()
        )
        recovered = self.store.recover_stale()
        for approval_id in recovery_approval_ids:
            await self._emit_approval_updated(approval_id)
        self._closing = False
        self.executor.start()
        self._started = True
        queued = self.store.list_queued_instance_ids()
        for instance_id in queued:
            self._launch_instance(instance_id)
        recovered["resumed_instances"] = len(queued)
        return recovered

    async def close(self) -> None:
        if not self._started:
            return
        self._closing = True
        self.executor.begin_shutdown()
        tasks = [task for task in self._instance_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.executor.close()
            await self.providers.close()
        finally:
            self._instance_tasks.clear()
            self._started = False

    def validate_definition(
        self,
        kind: str,
        definition: BaseModel | Mapping[str, Any],
    ) -> BaseModel:
        model = self.models.get(kind)
        parsed = model.parse_definition(definition)
        model.validate_definition(
            parsed,
            validate_agent_task=self.executor.validate_task,
        )
        return parsed

    def validate_workflow(self, workflow: WorkflowDefinition) -> None:
        self.validate_definition(OrchestrationKind.dag.value, workflow)

    def set_event_hooks(self, hooks: WorkflowEventHooks | None) -> None:
        self.event_hooks = hooks

    async def submit(
        self,
        definition: BaseModel | Mapping[str, Any],
        *,
        kind: str = OrchestrationKind.dag.value,
        template_id: str | None = None,
        template_version: int | None = None,
        input_data: dict[str, Any] | None = None,
        cause_type: str = "manual",
        trigger_binding_id: str | None = None,
        trigger_event_id: str | None = None,
    ) -> str:
        if not self._started:
            await self.start()
        parsed = self.validate_definition(kind, definition)
        model = self.models.get(kind)
        instance_id, created = self.store.create_instance(
            kind=kind,
            definition_schema_version=model.definition_schema_version,
            name=model.display_name(parsed),
            definition=parsed.model_dump(mode="json"),
            work_items=model.materialize_work_items(parsed),
            template_id=template_id,
            template_version=template_version,
            input_data=input_data or {},
            cause_type=cause_type,
            trigger_binding_id=trigger_binding_id,
            trigger_event_id=trigger_event_id,
            enqueue_created_event=True,
        )
        if created:
            self.store.append_event(
                instance_id=instance_id,
                event=ProviderEvent(
                    kind=EventKind.started,
                    summary="Workflow queued",
                    payload={
                        "kind": kind,
                        "template_id": template_id,
                        "template_version": template_version,
                        "source": "template" if template_id else "ad_hoc",
                        "cause_type": cause_type,
                        "trigger_binding_id": trigger_binding_id,
                        "trigger_event_id": trigger_event_id,
                    },
                    raw_event_type="orchestrator.instance_queued",
                ),
            )
            await self._emit_instance_created(instance_id)
            self._launch_instance(instance_id)
        return instance_id

    def _launch_instance(self, instance_id: str) -> None:
        existing = self._instance_tasks.get(instance_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_instance(instance_id),
            name=f"multi-agent-instance-{instance_id}",
        )
        self._instance_tasks[instance_id] = task

    async def _run_instance(self, instance_id: str) -> None:
        try:
            instance = self.store.get_instance(instance_id)
            model = self.models.get(instance["kind"])
            definition = model.parse_definition(instance["definition"])
            await model.run(
                definition,
                OrchestrationRuntimeContext(
                    instance_id=instance_id,
                    store=self.store,
                    execute_agent_task=self.executor.execute,
                    is_closing=lambda: self._closing,
                    emit_instance_status_changed=(
                        self._emit_instance_status_changed
                    ),
                    emit_approval_updated=self._emit_approval_updated,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            previous = self.store.get_instance(instance_id)
            self.store.set_instance_status(
                instance_id,
                WorkflowInstanceStatus.failed,
                error=str(exc),
                internal_event=TriggerEventInput(
                    source_type="internal",
                    event_type="workflow.instance.status_changed",
                    event_version=1,
                    source_key=instance_id,
                    dedup_key=(
                        f"workflow-instance-status:{instance_id}:"
                        f"{WorkflowInstanceStatus.failed.value}:"
                        f"{previous['revision'] + 1}"
                    ),
                    payload={
                        "workflow_instance_id": instance_id,
                        "old_status": str(previous["status"]),
                        "new_status": WorkflowInstanceStatus.failed.value,
                        "revision": previous["revision"] + 1,
                        "error": str(exc),
                    },
                ),
            )
            current = self.store.get_instance(instance_id)
            await self._emit_instance_status_changed(
                instance_id,
                str(previous["status"]),
                current["status"],
                int(current["revision"]),
                current["error"],
            )
            self.store.append_event(
                instance_id=instance_id,
                event=ProviderEvent(
                    kind=EventKind.failed,
                    summary=str(exc),
                    payload={"code": "orchestration_runtime_error"},
                    raw_event_type="orchestrator.runtime_failed",
                ),
            )
        finally:
            self._instance_tasks.pop(instance_id, None)

    async def _emit_instance_created(self, instance_id: str) -> None:
        hooks = self.event_hooks
        if hooks is None:
            return
        try:
            await hooks.workflow_instance_created(instance_id)
        except Exception:
            return

    async def _emit_approval_updated(self, approval_id: str) -> None:
        hooks = self.event_hooks
        if hooks is None:
            return
        try:
            await hooks.approval_updated(approval_id)
        except Exception:
            return

    async def _emit_instance_status_changed(
        self,
        instance_id: str,
        old_status: str,
        new_status: str,
        revision: int,
        error: str | None,
    ) -> None:
        hooks = self.event_hooks
        if hooks is None:
            return
        try:
            await hooks.workflow_instance_status_changed(
                instance_id,
                old_status=old_status,
                new_status=new_status,
                revision=revision,
                error=error,
            )
        except Exception:
            return

    async def wait(self, instance_id: str) -> dict[str, Any]:
        task = self._instance_tasks.get(instance_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        return self.store.get_instance(instance_id)

    async def cancel_instance(self, instance_id: str) -> dict[str, Any]:
        instance = self.store.get_instance(instance_id)
        if instance["status"] in {
            WorkflowInstanceStatus.succeeded.value,
            WorkflowInstanceStatus.failed.value,
            WorkflowInstanceStatus.cancelled.value,
            WorkflowInstanceStatus.interrupted.value,
        }:
            return instance
        task = self._instance_tasks.get(instance_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.store.get_instance(instance_id)

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return await self.executor.resolve_approval(
            approval_id,
            approved=approved,
            decided_by=decided_by,
            reason=reason,
        )
