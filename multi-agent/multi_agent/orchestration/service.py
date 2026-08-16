from __future__ import annotations

from typing import Any

from multi_agent.domain.errors import WorkflowTemplateVersionConflictError
from multi_agent.domain.models import (
    ApprovalStatus,
    OrchestrationKind,
    ScheduledTaskDefinition,
    TriggerBindingDefinition,
    TriggerEventInput,
    WorkflowDefinition,
    WorkflowInstanceStatus,
)
from multi_agent.orchestration.engine import WorkflowEngine
from multi_agent.scheduling.service import PersistentSchedulerService
from multi_agent.triggers.events import (
    EventTypeRegistry,
    default_event_type_registry,
)
from multi_agent.triggers.internal import InternalEventPublisher
from multi_agent.triggers.service import TriggerService
from multi_agent.triggers.sources import (
    EventSourceRegistry,
    GitCommitEventSource,
    InternalEventSource,
    ManualEventSource,
    ScheduleEventSource,
    WebhookEventSource,
)


class OrchestrationApplicationService:
    """Single application boundary for templates, instances and event ingress."""

    def __init__(
        self,
        engine: WorkflowEngine,
        *,
        event_sources: EventSourceRegistry | None = None,
        event_types: EventTypeRegistry | None = None,
    ) -> None:
        self.engine = engine
        self.store = engine.store
        self.event_sources = event_sources or EventSourceRegistry(
            [
                GitCommitEventSource(workspaces=engine.workspaces),
                ManualEventSource(),
                WebhookEventSource(),
                InternalEventSource(),
                ScheduleEventSource(),
            ]
        )
        self.event_types = event_types or default_event_type_registry()
        self.triggers = TriggerService(
            store=self.store,
            sources=self.event_sources,
            event_types=self.event_types,
            target=self,
        )
        self.internal_events = InternalEventPublisher(
            store=self.store,
            triggers=self.triggers,
        )
        engine.set_event_hooks(self.internal_events)
        engine.executor.set_approval_hook(
            self.internal_events.approval_updated
        )
        self.scheduler = PersistentSchedulerService(
            store=self.store,
            triggers=self.triggers,
        )

    async def start(self) -> dict[str, int]:
        recovered = await self.engine.start()
        recovered["internal_event_outbox"] = (
            await self.triggers.recover_internal_outbox()
        )
        recovered["trigger_deliveries"] = (
            await self.triggers.recover_pending_deliveries()
        )
        recovered.update(await self.scheduler.start())
        return recovered

    async def close(self) -> None:
        await self.scheduler.close()
        await self.engine.close()

    def describe_models(self) -> list[dict[str, object]]:
        return self.engine.models.describe()

    def describe_event_sources(self) -> list[dict[str, object]]:
        return self.event_sources.describe()

    def describe_event_types(self) -> list[dict[str, Any]]:
        return self.event_types.describe()

    def describe_schedule_types(self) -> list[dict[str, Any]]:
        return self.scheduler.describe_schedule_types()

    def describe_scheduled_action_types(self) -> list[dict[str, Any]]:
        return self.scheduler.describe_action_types()

    async def describe_providers(self) -> list[dict[str, object]]:
        return await self.engine.providers.describe()

    def describe_workspaces(self) -> dict[str, str]:
        return self.engine.workspaces.describe()

    def validate_template(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        result = self.validate_orchestration_definition(
            OrchestrationKind.dag.value, workflow
        )
        return {**result, "task_count": result["work_item_count"]}

    def validate_orchestration_definition(
        self,
        kind: str,
        definition: Any,
    ) -> dict[str, Any]:
        parsed = self.engine.validate_definition(kind, definition)
        model = self.engine.models.get(kind)
        return {
            "valid": True,
            "kind": model.kind,
            "definition_schema_version": model.definition_schema_version,
            "template_id": model.definition_id(parsed),
            "work_item_count": len(model.materialize_work_items(parsed)),
        }

    def create_template(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        return self.create_orchestration_template(
            OrchestrationKind.dag.value, workflow
        )

    def create_orchestration_template(
        self,
        kind: str,
        definition: Any,
    ) -> dict[str, Any]:
        parsed = self.engine.validate_definition(kind, definition)
        model = self.engine.models.get(kind)
        record = self.store.create_template(
            template_id=model.definition_id(parsed),
            version=model.definition_version(parsed),
            kind=model.kind,
            definition_schema_version=model.definition_schema_version,
            name=model.display_name(parsed),
            definition=parsed.model_dump(mode="json"),
            work_item_count=len(model.materialize_work_items(parsed)),
        )
        return self._present_template(record)

    def update_template(
        self,
        template_id: str,
        workflow: WorkflowDefinition,
    ) -> dict[str, Any]:
        return self.update_orchestration_template(
            template_id,
            OrchestrationKind.dag.value,
            workflow,
        )

    def update_orchestration_template(
        self,
        template_id: str,
        kind: str,
        definition: Any,
    ) -> dict[str, Any]:
        parsed = self.engine.validate_definition(kind, definition)
        model = self.engine.models.get(kind)
        if model.definition_id(parsed) != template_id:
            raise WorkflowTemplateVersionConflictError(
                "workflow template body id must match the path id"
            )
        expected_version = model.definition_version(parsed)
        next_definition = model.with_definition_version(
            parsed, expected_version + 1
        )
        parsed_next = self.engine.validate_definition(
            kind, next_definition
        )
        record = self.store.update_template(
            template_id,
            expected_version=expected_version,
            kind=model.kind,
            definition_schema_version=model.definition_schema_version,
            name=model.display_name(parsed_next),
            definition=parsed_next.model_dump(mode="json"),
            work_item_count=len(model.materialize_work_items(parsed_next)),
        )
        return self._present_template(record)

    def get_template(self, template_id: str) -> dict[str, Any]:
        return self._present_template(self.store.get_template(template_id))

    def list_templates(self, **kwargs: Any) -> dict[str, Any]:
        page = self.store.list_templates(**kwargs)
        return {
            **page,
            "items": [self._present_template(item) for item in page["items"]],
        }

    def archive_template(self, template_id: str) -> dict[str, Any]:
        return self._present_template(self.store.archive_template(template_id))

    async def instantiate_template(
        self,
        template_id: str,
        *,
        input_data: dict[str, Any] | None = None,
        trigger_binding_id: str | None = None,
        trigger_event_id: str | None = None,
    ) -> dict[str, Any]:
        template = self.store.get_template(template_id)
        cause_type = "trigger" if trigger_event_id is not None else "manual"
        instance_id = await self.engine.submit(
            template["definition"],
            kind=template["kind"],
            template_id=template["id"],
            template_version=template["version"],
            input_data=input_data,
            cause_type=cause_type,
            trigger_binding_id=trigger_binding_id,
            trigger_event_id=trigger_event_id,
        )
        return self.get_instance(instance_id)

    async def submit_ad_hoc(
        self,
        workflow: WorkflowDefinition,
        *,
        input_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.submit_ad_hoc_definition(
            OrchestrationKind.dag.value,
            workflow,
            input_data=input_data,
        )

    async def submit_ad_hoc_definition(
        self,
        kind: str,
        definition: Any,
        *,
        input_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        instance_id = await self.engine.submit(
            definition,
            kind=kind,
            input_data=input_data,
        )
        return self.get_instance(instance_id)

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        return self._present_instance(self.store.get_instance(instance_id))

    def list_instances(
        self,
        *,
        limit: int,
        cursor: str | None,
        status: WorkflowInstanceStatus | None,
    ) -> dict[str, Any]:
        page = self.store.list_instances(
            limit=limit, cursor=cursor, status=status
        )
        return {
            **page,
            "items": [self._present_instance(item) for item in page["items"]],
        }

    def list_work_items(self, instance_id: str) -> list[dict[str, Any]]:
        return self.store.list_work_items(instance_id)

    def list_dag_tasks(self, instance_id: str) -> list[dict[str, Any]]:
        instance = self.store.get_instance(instance_id)
        if instance["kind"] != OrchestrationKind.dag.value:
            raise ValueError("the instance does not use the DAG model")
        return [
            {**item, "task_id": item["logical_key"]}
            for item in self.store.list_work_items(instance_id)
        ]

    async def cancel_instance(self, instance_id: str) -> dict[str, Any]:
        await self.engine.cancel_instance(instance_id)
        return self.get_instance(instance_id)

    def list_approvals(
        self,
        instance_id: str,
        *,
        status: ApprovalStatus | None,
    ) -> list[dict[str, Any]]:
        return self.store.list_approvals(instance_id, status=status)

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        reason: str | None,
    ) -> dict[str, Any]:
        return await self.engine.resolve_approval(
            approval_id,
            approved=approved,
            decided_by=decided_by,
            reason=reason,
        )

    def create_trigger_binding(
        self,
        binding: TriggerBindingDefinition,
    ) -> dict[str, Any]:
        return self.triggers.create_binding(binding)

    def update_trigger_binding(
        self,
        binding_id: str,
        binding: TriggerBindingDefinition,
    ) -> dict[str, Any]:
        record = self.triggers.update_binding(binding_id, binding)
        self.scheduler.refresh_tasks_for_binding(binding_id)
        return record

    def get_trigger_binding(self, binding_id: str) -> dict[str, Any]:
        return self.store.get_trigger_binding(binding_id)

    def list_trigger_bindings(
        self,
        *,
        include_archived: bool,
    ) -> list[dict[str, Any]]:
        return self.store.list_trigger_bindings(
            include_archived=include_archived
        )

    def archive_trigger_binding(self, binding_id: str) -> dict[str, Any]:
        record = self.store.archive_trigger_binding(binding_id)
        self.scheduler.refresh_tasks_for_binding(binding_id)
        return record

    def set_trigger_binding_enabled(
        self,
        binding_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        record = self.store.set_trigger_binding_enabled(binding_id, enabled)
        self.scheduler.refresh_tasks_for_binding(binding_id)
        return record

    async def poll_trigger_binding(self, binding_id: str) -> dict[str, Any]:
        return await self.triggers.poll_binding(binding_id)

    async def publish_trigger_event(
        self,
        event: TriggerEventInput,
    ) -> dict[str, Any]:
        return await self.triggers.publish(event)

    def webhook_payload_limit(self, endpoint_key: str) -> int:
        return self.triggers.webhook_payload_limit(endpoint_key)

    async def receive_webhook(
        self,
        endpoint_key: str,
        *,
        headers: dict[str, str],
        raw_body: bytes,
        client_ip: str | None,
    ) -> dict[str, Any]:
        return await self.triggers.receive_webhook(
            endpoint_key,
            headers=headers,
            raw_body=raw_body,
            client_ip=client_ip,
        )

    async def retry_trigger_event(self, event_id: str) -> dict[str, Any]:
        return await self.triggers.retry(event_id)

    def list_trigger_events(self, *, limit: int) -> list[dict[str, Any]]:
        return self.store.list_trigger_events(limit=limit)

    def get_trigger_event(self, event_id: str) -> dict[str, Any]:
        event = self.store.get_trigger_event(event_id)
        return {
            **event,
            "deliveries": self.store.list_trigger_deliveries(event_id),
        }

    def create_scheduled_task(
        self,
        definition: ScheduledTaskDefinition,
    ) -> dict[str, Any]:
        return self.scheduler.create_task(definition)

    def update_scheduled_task(
        self,
        task_id: str,
        definition: ScheduledTaskDefinition,
    ) -> dict[str, Any]:
        return self.scheduler.update_task(task_id, definition)

    def get_scheduled_task(self, task_id: str) -> dict[str, Any]:
        return self.scheduler.get_task(task_id)

    def list_scheduled_tasks(
        self,
        *,
        include_archived: bool,
        enabled: bool | None,
    ) -> list[dict[str, Any]]:
        return self.scheduler.list_tasks(
            include_archived=include_archived,
            enabled=enabled,
        )

    def set_scheduled_task_enabled(
        self,
        task_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        return self.scheduler.set_task_enabled(task_id, enabled)

    def archive_scheduled_task(self, task_id: str) -> dict[str, Any]:
        return self.scheduler.archive_task(task_id)

    async def run_scheduled_task(self, task_id: str) -> dict[str, Any]:
        return await self.scheduler.run_now(task_id)

    def list_scheduled_task_runs(
        self,
        task_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        return self.scheduler.list_runs(task_id, limit=limit)

    def list_instance_events(self, instance_id: str, *, after_id: int = 0):
        return self.store.list_events(instance_id, after_id=after_id)

    @staticmethod
    def _present_template(record: dict[str, Any]) -> dict[str, Any]:
        if record["kind"] == OrchestrationKind.dag.value:
            return {**record, "task_count": record["work_item_count"]}
        return record

    @staticmethod
    def _present_instance(record: dict[str, Any]) -> dict[str, Any]:
        if record["kind"] == OrchestrationKind.dag.value:
            return {
                **record,
                "task_count": record["work_item_count"],
                "completed_task_count": record["completed_work_item_count"],
            }
        return record
