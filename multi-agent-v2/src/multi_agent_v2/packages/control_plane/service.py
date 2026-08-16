from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

import jmespath
from jmespath.exceptions import JMESPathError
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from multi_agent_v2.packages.control_plane.models import (
    ApprovalRecord,
    GitRefTarget,
    InstanceRecord,
    InstanceStart,
    NodeProjectionRecord,
    ProviderCatalogRecord,
    ScheduleCreate,
    ScheduleRecord,
    ScheduleUpdate,
    TemplateCreate,
    TemplateRecord,
    TemplateVersionCreate,
    TemplateVersionRecord,
    TriggerCreate,
    TriggerRecord,
    TriggerUpdate,
    WorkflowEventRecord,
)
from multi_agent_v2.packages.control_plane.schedule_adapter import (
    ScheduleContractError,
    validate_schedule_record,
)
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.persistence import ControlPlaneRepository
from multi_agent_v2.packages.workflow_dsl import (
    CompilationContext,
    compile_workflow,
    parse_json_workflow,
)


class WorkflowCatalog(Protocol):
    async def compilation_context(self) -> CompilationContext: ...

    def workspace_ids(self) -> tuple[str, ...]: ...


class StaticWorkflowCatalog:
    def __init__(self, context: CompilationContext) -> None:
        self._context = context

    async def compilation_context(self) -> CompilationContext:
        return self._context

    def workspace_ids(self) -> tuple[str, ...]:
        return self._context.workspace_ids


class WorkflowInputContractError(ValueError):
    """Workflow input does not satisfy the compiled input schema."""


class TriggerContractError(ValueError):
    """A trigger binding expression is invalid."""


class ControlPlaneService:
    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        catalog: WorkflowCatalog,
    ) -> None:
        self._repository = repository
        self._catalog = catalog

    async def create_template(
        self,
        command: TemplateCreate,
        *,
        idempotency_key: str,
    ) -> TemplateRecord:
        return await self._repository.create_template(
            command,
            idempotency_key=idempotency_key,
        )

    async def create_template_version(
        self,
        template_id: str,
        command: TemplateVersionCreate,
        *,
        idempotency_key: str,
    ) -> TemplateVersionRecord:
        definition = parse_json_workflow(
            json.dumps(
                command.definition,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        context = await self._catalog.compilation_context()
        plan = compile_workflow(definition, context)
        return await self._repository.create_template_version(
            template_id=template_id,
            definition=cast(
                JsonObject,
                definition.model_dump(mode="json", by_alias=True),
            ),
            plan=plan,
            idempotency_key=idempotency_key,
        )

    async def start_instance(
        self,
        template_id: str,
        command: InstanceStart,
        *,
        idempotency_key: str,
    ) -> InstanceRecord:
        template = await self._repository.get_template(template_id)
        version = command.template_version or template.latest_version
        if version < 1:
            raise WorkflowInputContractError("workflow template has no executable version")
        template_version = await self._repository.get_template_version(template_id, version)
        schema = cast(
            JsonObject,
            json.loads(template_version.compiled_plan.input_schema.canonical),
        )
        try:
            Draft202012Validator(schema).validate(command.workflow_input)  # pyright: ignore[reportUnknownMemberType]
        except ValidationError as exc:
            raise WorkflowInputContractError(
                "workflow input does not satisfy the compiled input schema"
            ) from exc
        return await self._repository.create_instance(
            template_id=template_id,
            template_version=version,
            command=command,
            idempotency_key=idempotency_key,
        )

    async def create_trigger(
        self,
        command: TriggerCreate,
        *,
        idempotency_key: str,
    ) -> TriggerRecord:
        _validate_trigger_bindings(command.input_bindings)
        return await self._repository.create_trigger(
            command,
            idempotency_key=idempotency_key,
        )

    async def update_trigger(
        self,
        trigger_id: str,
        command: TriggerUpdate,
        *,
        idempotency_key: str,
    ) -> TriggerRecord:
        _validate_trigger_bindings(command.input_bindings)
        return await self._repository.update_trigger(
            trigger_id,
            command,
            idempotency_key=idempotency_key,
        )

    async def create_schedule(
        self,
        command: ScheduleCreate,
        *,
        idempotency_key: str,
    ) -> ScheduleRecord:
        _validate_schedule(
            schedule_id=command.schedule_id,
            revision=1,
            name=command.name,
            enabled=command.enabled,
            schedule_kind=command.schedule_kind,
            schedule_spec=command.schedule_spec,
            target_kind=command.target_kind,
            target=command.target,
        )
        await self._validate_schedule_references(
            target_kind=command.target_kind,
            target=command.target,
        )
        return await self._repository.create_schedule(
            command,
            idempotency_key=idempotency_key,
        )

    async def update_schedule(
        self,
        schedule_id: str,
        command: ScheduleUpdate,
        *,
        idempotency_key: str,
    ) -> ScheduleRecord:
        _validate_schedule(
            schedule_id=schedule_id,
            revision=command.expected_revision + 1,
            name=command.name,
            enabled=command.enabled,
            schedule_kind=command.schedule_kind,
            schedule_spec=command.schedule_spec,
            target_kind=command.target_kind,
            target=command.target,
        )
        await self._validate_schedule_references(
            target_kind=command.target_kind,
            target=command.target,
        )
        return await self._repository.update_schedule(
            schedule_id,
            command,
            idempotency_key=idempotency_key,
        )

    async def get_template(self, template_id: str) -> TemplateRecord:
        return await self._repository.get_template(template_id)

    async def list_templates(self, *, limit: int) -> tuple[TemplateRecord, ...]:
        return await self._repository.list_templates(limit=limit)

    async def get_template_version(
        self,
        template_id: str,
        version: int,
    ) -> TemplateVersionRecord:
        return await self._repository.get_template_version(template_id, version)

    async def list_template_versions(
        self,
        template_id: str,
        *,
        limit: int,
    ) -> tuple[TemplateVersionRecord, ...]:
        return await self._repository.list_template_versions(template_id, limit=limit)

    async def get_instance(self, instance_id: str) -> InstanceRecord:
        return await self._repository.get_instance(instance_id)

    async def list_instances(
        self,
        *,
        statuses: tuple[str, ...],
        limit: int,
    ) -> tuple[InstanceRecord, ...]:
        return await self._repository.list_instances(statuses=statuses, limit=limit)

    async def list_instance_nodes(
        self,
        instance_id: str,
    ) -> tuple[NodeProjectionRecord, ...]:
        return await self._repository.list_instance_nodes(instance_id)

    async def get_approval(self, approval_id: str) -> ApprovalRecord:
        return await self._repository.get_approval(approval_id)

    async def list_approvals(
        self,
        *,
        instance_id: str | None,
        pending_only: bool,
        limit: int,
    ) -> tuple[ApprovalRecord, ...]:
        return await self._repository.list_approvals(
            instance_id=instance_id,
            pending_only=pending_only,
            limit=limit,
        )

    async def list_events(
        self,
        *,
        after_delivery_id: int,
        instance_id: str | None,
        limit: int,
    ) -> tuple[WorkflowEventRecord, ...]:
        return await self._repository.list_workflow_events(
            after_delivery_id=after_delivery_id,
            instance_id=instance_id,
            limit=limit,
        )

    async def get_provider_catalog(self) -> ProviderCatalogRecord:
        return await self._repository.get_provider_catalog()

    async def list_triggers(self, *, limit: int) -> tuple[TriggerRecord, ...]:
        return await self._repository.list_triggers(limit=limit)

    async def list_schedules(self, *, limit: int) -> tuple[ScheduleRecord, ...]:
        return await self._repository.list_schedules(limit=limit)

    async def _validate_schedule_references(
        self,
        *,
        target_kind: Literal["workflow", "git_connector"],
        target: JsonObject,
    ) -> None:
        if target_kind == "workflow":
            template_id = cast(str, target["templateId"])
            template_version = cast(int, target["templateVersion"])
            await self._repository.get_template_version(template_id, template_version)
            return
        connector = GitRefTarget.model_validate(target)
        if connector.workspace_id not in self._catalog.workspace_ids():
            raise ScheduleContractError("Git connector schedule references an unknown workspace")


def _validate_trigger_bindings(bindings: dict[str, str]) -> None:
    try:
        for expression in bindings.values():
            jmespath.compile(expression)
    except JMESPathError as exc:
        raise TriggerContractError("trigger input binding is invalid") from exc


def _validate_schedule(
    *,
    schedule_id: str,
    revision: int,
    name: str,
    enabled: bool,
    schedule_kind: Literal["cron", "interval", "calendar"],
    schedule_spec: JsonObject,
    target_kind: Literal["workflow", "git_connector"],
    target: JsonObject,
) -> None:
    now = datetime.now(UTC)
    validate_schedule_record(
        ScheduleRecord(
            schedule_id=schedule_id,
            name=name,
            revision=revision,
            enabled=enabled,
            schedule_kind=schedule_kind,
            schedule_spec=schedule_spec,
            target_kind=target_kind,
            target=target,
            created_at=now,
            updated_at=now,
        )
    )
