from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import jmespath
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel
from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from multi_agent_v2.packages.control_plane.models import (
    ApprovalRecord,
    CatalogModelRecord,
    EventWaitRegistration,
    InstanceRecord,
    InstanceStart,
    NodeProjectionRecord,
    OutboxCommand,
    ProjectionEvent,
    ProjectionNodeState,
    ProviderCatalogRecord,
    ScheduleCreate,
    ScheduleFireRequest,
    ScheduleRecord,
    ScheduleUpdate,
    TemplateCreate,
    TemplateRecord,
    TemplateVersionRecord,
    TriggerCreate,
    TriggerRecord,
    TriggerUpdate,
    WorkflowEventRecord,
    WorkflowSnapshotProjection,
)
from multi_agent_v2.packages.domain.events import CloudEventEnvelope, EventIngestResult
from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue
from multi_agent_v2.packages.persistence.control_models import (
    ApprovalProjection,
    AuditLog,
    CommandOutbox,
    ConnectorCheckpoint,
    EventInbox,
    EventWaitSubscription,
    IdempotencyRecord,
    ProviderCatalogSnapshot,
    ScheduleDefinition,
    TriggerDefinition,
    TriggerDelivery,
    WorkflowEvent,
    WorkflowInstanceProjection,
    WorkflowNodeProjection,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from multi_agent_v2.packages.workflow_dsl import ExecutablePlan
from multi_agent_v2.packages.workflow_dsl.canonical import canonical_json, sha256_text

_INSTANCE_NAMESPACE = UUID("c09b9c96-759e-45cc-9d3f-e4ab65f2ee69")
_COMMAND_NAMESPACE = UUID("8cbfbfe0-5431-47fc-9b7f-20f55b42815e")
_EVENT_NAMESPACE = UUID("49e590d6-4467-4667-a5e5-5e6b00ccba59")


class ControlPlaneRepositoryError(RuntimeError):
    """Base class for durable control-plane repository failures."""


class ControlPlaneNotFound(ControlPlaneRepositoryError):
    """The requested control-plane entity does not exist."""


class ControlPlaneConflict(ControlPlaneRepositoryError):
    """The requested identity is already owned by a different entity."""


class IdempotencyConflict(ControlPlaneConflict):
    """An idempotency key was reused with a different logical request."""


class RevisionConflict(ControlPlaneConflict):
    """An optimistic revision no longer matches."""


class OutboxLeaseLost(ControlPlaneConflict):
    """The caller no longer owns the fenced outbox claim."""


@dataclass(frozen=True, slots=True)
class OutboxFailure:
    status: str
    attempts: int
    available_at: datetime | None


@dataclass(frozen=True, slots=True)
class ConnectorAdvance:
    initialized: bool
    changed: bool
    previous_value: str | None
    current_value: str
    revision: int


@dataclass(frozen=True, slots=True)
class ConnectorCheckpointState:
    connector_id: str
    connector_kind: str
    configuration_hash: str
    checkpoint_value: str
    revision: int


class ControlPlaneRepository:
    """PostgreSQL source of truth for configuration, query projections, and commands."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_template(
        self,
        command: TemplateCreate,
        *,
        idempotency_key: str,
    ) -> TemplateRecord:
        request_hash = _request_hash(command)
        scope = "template.create"
        key = _idempotency_key(idempotency_key)
        async with self._sessions() as session, session.begin():
            replay = await _claim_idempotency(session, scope, key, request_hash)
            if replay is not None:
                return TemplateRecord.model_validate(replay)
            now = await _database_now(session)
            existing = await session.get(WorkflowTemplate, command.template_id)
            if existing is not None:
                raise ControlPlaneConflict("workflow template ID is already in use")
            row = WorkflowTemplate(
                template_id=command.template_id,
                name=command.name,
                description=command.description,
                latest_version=0,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            response = _template_record(row)
            await _complete_idempotency(session, scope, key, response)
            return response

    async def create_template_version(
        self,
        *,
        template_id: str,
        definition: JsonObject,
        plan: ExecutablePlan,
        idempotency_key: str,
    ) -> TemplateVersionRecord:
        request_hash = _request_hash(
            {
                "templateId": template_id,
                "definition": definition,
                "planHash": plan.plan_hash,
            }
        )
        scope = f"template.version.create:{template_id}"
        key = _idempotency_key(idempotency_key)
        async with self._sessions() as session, session.begin():
            replay = await _claim_idempotency(session, scope, key, request_hash)
            if replay is not None:
                return TemplateVersionRecord.model_validate(replay)
            template = await session.scalar(
                select(WorkflowTemplate)
                .where(WorkflowTemplate.template_id == template_id)
                .with_for_update()
            )
            if template is None:
                raise ControlPlaneNotFound("workflow template does not exist")
            next_version = template.latest_version + 1
            if plan.workflow_id != template_id or plan.workflow_version != next_version:
                raise ControlPlaneConflict(
                    "workflow definition metadata must match the template ID and next version"
                )
            now = await _database_now(session)
            row = WorkflowTemplateVersion(
                template_id=template_id,
                version=next_version,
                definition=definition,
                compiled_plan=plan.model_dump(mode="json", by_alias=True),
                plan_hash=plan.plan_hash,
                catalog_revision=plan.catalog_revision,
                created_at=now,
            )
            session.add(row)
            template.latest_version = next_version
            template.revision += 1
            template.updated_at = now
            response = _template_version_record(row)
            await _complete_idempotency(session, scope, key, response)
            return response

    async def get_template(self, template_id: str) -> TemplateRecord:
        async with self._sessions() as session:
            row = await session.get(WorkflowTemplate, template_id)
            if row is None:
                raise ControlPlaneNotFound("workflow template does not exist")
            return _template_record(row)

    async def list_templates(self, *, limit: int = 100) -> tuple[TemplateRecord, ...]:
        _validate_limit(limit)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(WorkflowTemplate)
                    .order_by(WorkflowTemplate.updated_at.desc(), WorkflowTemplate.template_id)
                    .limit(limit)
                )
            ).all()
            return tuple(_template_record(row) for row in rows)

    async def get_template_version(
        self,
        template_id: str,
        version: int,
    ) -> TemplateVersionRecord:
        async with self._sessions() as session:
            row = await session.get(
                WorkflowTemplateVersion,
                {"template_id": template_id, "version": version},
            )
            if row is None:
                raise ControlPlaneNotFound("workflow template version does not exist")
            return _template_version_record(row)

    async def list_template_versions(
        self,
        template_id: str,
        *,
        limit: int = 100,
    ) -> tuple[TemplateVersionRecord, ...]:
        _validate_limit(limit)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(WorkflowTemplateVersion)
                    .where(WorkflowTemplateVersion.template_id == template_id)
                    .order_by(WorkflowTemplateVersion.version.desc())
                    .limit(limit)
                )
            ).all()
            return tuple(_template_version_record(row) for row in rows)

    async def save_provider_catalog(
        self,
        *,
        runtime_name: str,
        runtime_id: str,
        provider_id: str,
        revision: str,
        models: tuple[CatalogModelRecord, ...],
    ) -> ProviderCatalogRecord:
        if len(revision) != 64:
            raise ValueError("provider catalog revision must be a SHA-256 hex digest")
        async with self._sessions() as session, session.begin():
            row = await session.scalar(
                select(ProviderCatalogSnapshot)
                .where(ProviderCatalogSnapshot.runtime_name == runtime_name)
                .with_for_update()
            )
            now = await _database_now(session)
            serialized_models = [model.model_dump(mode="json", by_alias=True) for model in models]
            if row is None:
                row = ProviderCatalogSnapshot(
                    runtime_name=runtime_name,
                    runtime_id=runtime_id,
                    provider_id=provider_id,
                    revision=revision,
                    models=serialized_models,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.runtime_id = runtime_id
                row.provider_id = provider_id
                row.revision = revision
                row.models = serialized_models
                row.updated_at = now
            return _provider_catalog_record(row)

    async def get_provider_catalog(
        self,
        runtime_name: str = "codex",
    ) -> ProviderCatalogRecord:
        async with self._sessions() as session:
            row = await session.get(ProviderCatalogSnapshot, runtime_name)
            if row is None:
                raise ControlPlaneNotFound("provider model catalog is not available")
            return _provider_catalog_record(row)

    async def create_instance(
        self,
        *,
        template_id: str,
        template_version: int,
        command: InstanceStart,
        idempotency_key: str,
    ) -> InstanceRecord:
        request_hash = _request_hash(
            {
                "templateId": template_id,
                "templateVersion": template_version,
                "command": command.model_dump(mode="json", by_alias=True),
            }
        )
        scope = f"workflow.instance.start:{template_id}:{template_version}"
        key = _idempotency_key(idempotency_key)
        instance_id = str(uuid5(_INSTANCE_NAMESPACE, f"{scope}\0{key}"))
        workflow_id = f"multi-agent-v2/instances/{instance_id}"
        command_id = str(uuid5(_COMMAND_NAMESPACE, f"workflow.start.v1\0{instance_id}"))
        async with self._sessions() as session, session.begin():
            replay = await _claim_idempotency(session, scope, key, request_hash)
            if replay is not None:
                return InstanceRecord.model_validate(replay)
            version = await session.get(
                WorkflowTemplateVersion,
                {"template_id": template_id, "version": template_version},
            )
            if version is None:
                raise ControlPlaneNotFound("workflow template version does not exist")
            now = await _database_now(session)
            row = WorkflowInstanceProjection(
                instance_id=instance_id,
                template_id=template_id,
                template_version=template_version,
                temporal_workflow_id=workflow_id,
                status="pending_start",
                workflow_input=command.workflow_input,
                trigger_cause=command.trigger_cause,
                projection_version=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.add(
                CommandOutbox(
                    outbox_id=str(uuid4()),
                    command_id=command_id,
                    command_type="workflow.start.v1",
                    aggregate_type="workflow_instance",
                    aggregate_id=instance_id,
                    payload={
                        "instanceId": instance_id,
                        "templateId": template_id,
                        "templateVersion": template_version,
                        "temporalWorkflowId": workflow_id,
                        "workflowInput": command.workflow_input,
                    },
                    status="pending",
                    attempts=0,
                    available_at=now,
                    lease_epoch=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            response = _instance_record(row)
            await _complete_idempotency(session, scope, key, response)
            return response

    async def get_instance(self, instance_id: str) -> InstanceRecord:
        async with self._sessions() as session:
            row = await session.get(WorkflowInstanceProjection, instance_id)
            if row is None:
                raise ControlPlaneNotFound("workflow instance does not exist")
            return _instance_record(row)

    async def list_instance_nodes(
        self,
        instance_id: str,
    ) -> tuple[NodeProjectionRecord, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(WorkflowNodeProjection)
                    .where(WorkflowNodeProjection.instance_id == instance_id)
                    .order_by(
                        WorkflowNodeProjection.node_id,
                        WorkflowNodeProjection.activation.desc(),
                    )
                )
            ).all()
            return tuple(_node_projection_record(row) for row in rows)

    async def get_approval(self, approval_id: str) -> ApprovalRecord:
        async with self._sessions() as session:
            row = await session.get(ApprovalProjection, approval_id)
            if row is None:
                raise ControlPlaneNotFound("approval does not exist")
            return _approval_record(row)

    async def list_approvals(
        self,
        *,
        instance_id: str | None = None,
        pending_only: bool = False,
        limit: int = 100,
    ) -> tuple[ApprovalRecord, ...]:
        _validate_limit(limit)
        statement: Select[tuple[ApprovalProjection]] = select(ApprovalProjection)
        if instance_id is not None:
            statement = statement.where(ApprovalProjection.instance_id == instance_id)
        if pending_only:
            statement = statement.where(ApprovalProjection.status == "pending")
        statement = statement.order_by(
            ApprovalProjection.requested_at.desc(),
            ApprovalProjection.approval_id,
        ).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(_approval_record(row) for row in rows)

    async def list_workflow_events(
        self,
        *,
        after_delivery_id: int = 0,
        instance_id: str | None = None,
        limit: int = 1000,
    ) -> tuple[WorkflowEventRecord, ...]:
        if after_delivery_id < 0:
            raise ValueError("workflow event cursor must be non-negative")
        _validate_limit(limit)
        statement: Select[tuple[WorkflowEvent]] = select(WorkflowEvent).where(
            WorkflowEvent.delivery_id > after_delivery_id
        )
        if instance_id is not None:
            statement = statement.where(WorkflowEvent.instance_id == instance_id)
        statement = statement.order_by(WorkflowEvent.delivery_id).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(_workflow_event_record(row) for row in rows)

    async def list_instances(
        self,
        *,
        statuses: Sequence[str] = (),
        limit: int = 100,
    ) -> tuple[InstanceRecord, ...]:
        _validate_limit(limit)
        statement: Select[tuple[WorkflowInstanceProjection]] = select(WorkflowInstanceProjection)
        if statuses:
            statement = statement.where(WorkflowInstanceProjection.status.in_(statuses))
        statement = statement.order_by(
            WorkflowInstanceProjection.updated_at.desc(),
            WorkflowInstanceProjection.instance_id,
        ).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(_instance_record(row) for row in rows)

    async def create_trigger(
        self,
        command: TriggerCreate,
        *,
        idempotency_key: str,
    ) -> TriggerRecord:
        request_hash = _request_hash(command)
        scope = "trigger.create"
        key = _idempotency_key(idempotency_key)
        async with self._sessions() as session, session.begin():
            replay = await _claim_idempotency(session, scope, key, request_hash)
            if replay is not None:
                return TriggerRecord.model_validate(replay)
            if await session.get(TriggerDefinition, command.trigger_id) is not None:
                raise ControlPlaneConflict("trigger ID is already in use")
            await _require_template_version(
                session,
                command.template_id,
                command.template_version,
            )
            now = await _database_now(session)
            row = TriggerDefinition(
                trigger_id=command.trigger_id,
                name=command.name,
                revision=1,
                enabled=command.enabled,
                event_type=command.event_type,
                source_pattern=command.source_pattern,
                subject_pattern=command.subject_pattern,
                template_id=command.template_id,
                template_version=command.template_version,
                input_bindings=command.input_bindings,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            response = _trigger_record(row)
            await _complete_idempotency(session, scope, key, response)
            return response

    async def update_trigger(
        self,
        trigger_id: str,
        command: TriggerUpdate,
        *,
        idempotency_key: str,
    ) -> TriggerRecord:
        request_hash = _request_hash(
            {"triggerId": trigger_id, "command": command.model_dump(mode="json", by_alias=True)}
        )
        scope = f"trigger.update:{trigger_id}"
        key = _idempotency_key(idempotency_key)
        async with self._sessions() as session, session.begin():
            replay = await _claim_idempotency(session, scope, key, request_hash)
            if replay is not None:
                return TriggerRecord.model_validate(replay)
            row = await session.scalar(
                select(TriggerDefinition)
                .where(TriggerDefinition.trigger_id == trigger_id)
                .with_for_update()
            )
            if row is None:
                raise ControlPlaneNotFound("trigger does not exist")
            if row.revision != command.expected_revision:
                raise RevisionConflict("trigger revision does not match")
            await _require_template_version(
                session,
                command.template_id,
                command.template_version,
            )
            now = await _database_now(session)
            row.name = command.name
            row.enabled = command.enabled
            row.event_type = command.event_type
            row.source_pattern = command.source_pattern
            row.subject_pattern = command.subject_pattern
            row.template_id = command.template_id
            row.template_version = command.template_version
            row.input_bindings = command.input_bindings
            row.revision += 1
            row.updated_at = now
            response = _trigger_record(row)
            await _complete_idempotency(session, scope, key, response)
            return response

    async def list_triggers(self, *, limit: int = 100) -> tuple[TriggerRecord, ...]:
        _validate_limit(limit)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(TriggerDefinition)
                    .order_by(TriggerDefinition.updated_at.desc(), TriggerDefinition.trigger_id)
                    .limit(limit)
                )
            ).all()
            return tuple(_trigger_record(row) for row in rows)

    async def create_schedule(
        self,
        command: ScheduleCreate,
        *,
        idempotency_key: str,
    ) -> ScheduleRecord:
        request_hash = _request_hash(command)
        scope = "schedule.create"
        key = _idempotency_key(idempotency_key)
        async with self._sessions() as session, session.begin():
            replay = await _claim_idempotency(session, scope, key, request_hash)
            if replay is not None:
                return ScheduleRecord.model_validate(replay)
            if await session.get(ScheduleDefinition, command.schedule_id) is not None:
                raise ControlPlaneConflict("schedule ID is already in use")
            now = await _database_now(session)
            row = ScheduleDefinition(
                schedule_id=command.schedule_id,
                name=command.name,
                revision=1,
                enabled=command.enabled,
                schedule_kind=command.schedule_kind,
                schedule_spec=command.schedule_spec,
                target_kind=command.target_kind,
                target=command.target,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await _append_schedule_sync_command(session, row, now)
            response = _schedule_record(row)
            await _complete_idempotency(session, scope, key, response)
            return response

    async def update_schedule(
        self,
        schedule_id: str,
        command: ScheduleUpdate,
        *,
        idempotency_key: str,
    ) -> ScheduleRecord:
        request_hash = _request_hash(
            {"scheduleId": schedule_id, "command": command.model_dump(mode="json", by_alias=True)}
        )
        scope = f"schedule.update:{schedule_id}"
        key = _idempotency_key(idempotency_key)
        async with self._sessions() as session, session.begin():
            replay = await _claim_idempotency(session, scope, key, request_hash)
            if replay is not None:
                return ScheduleRecord.model_validate(replay)
            row = await session.scalar(
                select(ScheduleDefinition)
                .where(ScheduleDefinition.schedule_id == schedule_id)
                .with_for_update()
            )
            if row is None:
                raise ControlPlaneNotFound("schedule does not exist")
            if row.revision != command.expected_revision:
                raise RevisionConflict("schedule revision does not match")
            now = await _database_now(session)
            row.name = command.name
            row.enabled = command.enabled
            row.schedule_kind = command.schedule_kind
            row.schedule_spec = command.schedule_spec
            row.target_kind = command.target_kind
            row.target = command.target
            row.revision += 1
            row.updated_at = now
            await _append_schedule_sync_command(session, row, now)
            response = _schedule_record(row)
            await _complete_idempotency(session, scope, key, response)
            return response

    async def list_schedules(self, *, limit: int = 100) -> tuple[ScheduleRecord, ...]:
        _validate_limit(limit)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ScheduleDefinition)
                    .order_by(ScheduleDefinition.updated_at.desc(), ScheduleDefinition.schedule_id)
                    .limit(limit)
                )
            ).all()
            return tuple(_schedule_record(row) for row in rows)

    async def fire_schedule(self, request: ScheduleFireRequest) -> InstanceRecord | None:
        async with self._sessions() as session, session.begin():
            schedule = await session.scalar(
                select(ScheduleDefinition)
                .where(ScheduleDefinition.schedule_id == request.schedule_id)
                .with_for_update()
            )
            if (
                schedule is None
                or not schedule.enabled
                or schedule.revision != request.schedule_revision
                or schedule.target_kind != "workflow"
            ):
                return None
            if cast(JsonObject, schedule.target) != request.target:
                return None
            template_id, template_version, workflow_input = _workflow_schedule_target(
                request.target
            )
            version = await _require_template_version(
                session,
                template_id,
                template_version,
            )
            plan = ExecutablePlan.model_validate(version.compiled_plan)
            schema = cast(JsonObject, json.loads(plan.input_schema.canonical))
            try:
                Draft202012Validator(schema).validate(workflow_input)  # pyright: ignore[reportUnknownMemberType]
            except ValidationError as exc:
                raise ControlPlaneConflict(
                    "scheduled workflow input violates its compiled contract"
                ) from exc
            instance_id = str(
                uuid5(
                    _INSTANCE_NAMESPACE,
                    f"schedule\0{request.schedule_id}\0{request.occurrence_id}",
                )
            )
            existing = await session.get(WorkflowInstanceProjection, instance_id)
            if existing is not None:
                return _instance_record(existing)
            now = await _database_now(session)
            temporal_workflow_id = f"multi-agent-v2/instances/{instance_id}"
            row = WorkflowInstanceProjection(
                instance_id=instance_id,
                template_id=template_id,
                template_version=template_version,
                temporal_workflow_id=temporal_workflow_id,
                status="pending_start",
                workflow_input=workflow_input,
                trigger_cause={
                    "kind": "schedule",
                    "scheduleId": request.schedule_id,
                    "scheduleRevision": request.schedule_revision,
                    "occurrenceId": request.occurrence_id,
                },
                projection_version=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            command_id = str(uuid5(_COMMAND_NAMESPACE, f"workflow.start.v1\0{instance_id}"))
            session.add(
                CommandOutbox(
                    outbox_id=str(uuid4()),
                    command_id=command_id,
                    command_type="workflow.start.v1",
                    aggregate_type="workflow_instance",
                    aggregate_id=instance_id,
                    payload={
                        "instanceId": instance_id,
                        "templateId": template_id,
                        "templateVersion": template_version,
                        "temporalWorkflowId": temporal_workflow_id,
                        "workflowInput": workflow_input,
                    },
                    status="pending",
                    attempts=0,
                    available_at=now,
                    lease_epoch=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            return _instance_record(row)

    async def ingest_event(self, event: CloudEventEnvelope) -> EventIngestResult:
        inbox_id = str(uuid5(_EVENT_NAMESPACE, f"{event.source}\0{event.id}"))
        async with self._sessions() as session, session.begin():
            now = await _database_now(session)
            inserted = await session.scalar(
                pg_insert(EventInbox)
                .values(
                    inbox_id=inbox_id,
                    source=event.source,
                    event_id=event.id,
                    event_type=event.type,
                    subject=event.subject,
                    event_time=event.time,
                    data_content_type=event.datacontenttype,
                    data_schema=event.dataschema,
                    data=event.data,
                    extensions=event.extensions,
                    status="received",
                    received_at=now,
                )
                .on_conflict_do_nothing(index_elements=[EventInbox.source, EventInbox.event_id])
                .returning(EventInbox.inbox_id)
            )
            if inserted is None:
                return await _existing_ingest_result(session, event.source, event.id)
            inbox = await session.get(EventInbox, inbox_id)
            if inbox is None:
                raise ControlPlaneRepositoryError("event inbox row disappeared after insertion")

            event_document = _event_document(event)
            routed_instances: list[str] = []
            trigger_rows = (
                await session.scalars(
                    select(TriggerDefinition).where(
                        TriggerDefinition.enabled.is_(True),
                        TriggerDefinition.event_type == event.type,
                    )
                )
            ).all()
            for trigger in trigger_rows:
                if not _pattern_matches(trigger.source_pattern, event.source):
                    continue
                if not _pattern_matches(trigger.subject_pattern, event.subject):
                    continue
                instance_id = await _route_trigger(
                    session,
                    trigger=trigger,
                    inbox=inbox,
                    event=event,
                    event_document=event_document,
                    now=now,
                )
                if instance_id is not None:
                    routed_instances.append(instance_id)

            signalled_workflows: list[str] = []
            subscriptions = (
                await session.scalars(
                    select(EventWaitSubscription)
                    .where(
                        EventWaitSubscription.status == "active",
                        EventWaitSubscription.event_type == event.type,
                        or_(
                            EventWaitSubscription.expires_at.is_(None),
                            EventWaitSubscription.expires_at > now,
                        ),
                    )
                    .with_for_update()
                )
            ).all()
            correlation_key = _event_correlation_key(event)
            for subscription in subscriptions:
                if not _pattern_matches(subscription.source_pattern, event.source):
                    continue
                if not _pattern_matches(subscription.subject_pattern, event.subject):
                    continue
                if (
                    subscription.correlation_key is not None
                    and subscription.correlation_key != correlation_key
                ):
                    continue
                try:
                    validator = Draft202012Validator(cast(JsonObject, subscription.output_schema))
                    validator.validate(event.data)  # pyright: ignore[reportUnknownMemberType]
                except ValidationError:
                    continue
                command_id = str(
                    uuid5(
                        _COMMAND_NAMESPACE,
                        f"workflow.signal.v1\0{inbox_id}\0{subscription.subscription_id}",
                    )
                )
                session.add(
                    CommandOutbox(
                        outbox_id=str(uuid4()),
                        command_id=command_id,
                        command_type="workflow.signal.v1",
                        aggregate_type="workflow_instance",
                        aggregate_id=subscription.instance_id,
                        payload={
                            "instanceId": subscription.instance_id,
                            "temporalWorkflowId": subscription.temporal_workflow_id,
                            "signalName": "event.deliver.v1",
                            "commandId": command_id,
                            "nodeId": subscription.node_id,
                            "activation": subscription.activation,
                            "event": event.model_dump(mode="json", by_alias=True),
                        },
                        status="pending",
                        attempts=0,
                        available_at=now,
                        lease_epoch=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                subscription.status = "delivered"
                subscription.delivery_command_id = command_id
                subscription.delivered_inbox_id = inbox_id
                subscription.updated_at = now
                signalled_workflows.append(subscription.temporal_workflow_id)

            inbox.status = "routed" if routed_instances or signalled_workflows else "ignored"
            inbox.processed_at = now
            return EventIngestResult(
                inbox_id=inbox_id,
                duplicate=False,
                routed_instances=tuple(sorted(routed_instances)),
                signalled_workflows=tuple(sorted(signalled_workflows)),
            )

    async def register_event_wait(self, registration: EventWaitRegistration) -> None:
        async with self._sessions() as session, session.begin():
            now = await _database_now(session)
            await session.execute(
                pg_insert(EventWaitSubscription)
                .values(
                    subscription_id=registration.subscription_id,
                    instance_id=registration.instance_id,
                    temporal_workflow_id=registration.temporal_workflow_id,
                    node_id=registration.node_id,
                    activation=registration.activation,
                    event_type=registration.event_type,
                    source_pattern=registration.source_pattern,
                    subject_pattern=registration.subject_pattern,
                    correlation_key=registration.correlation_key,
                    output_schema=cast(
                        dict[str, Any],
                        json.loads(registration.output_schema.canonical),
                    ),
                    output_schema_hash=registration.output_schema.sha256,
                    status="active",
                    expires_at=registration.expires_at,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        EventWaitSubscription.instance_id,
                        EventWaitSubscription.node_id,
                        EventWaitSubscription.activation,
                    ]
                )
            )
            row = await session.scalar(
                select(EventWaitSubscription)
                .where(
                    EventWaitSubscription.instance_id == registration.instance_id,
                    EventWaitSubscription.node_id == registration.node_id,
                    EventWaitSubscription.activation == registration.activation,
                )
                .with_for_update()
            )
            if row is None:
                raise ControlPlaneRepositoryError(
                    "event wait subscription was not visible after registration"
                )
            expected = (
                registration.subscription_id,
                registration.temporal_workflow_id,
                registration.event_type,
                registration.source_pattern,
                registration.subject_pattern,
                registration.correlation_key,
                registration.output_schema.sha256,
                registration.expires_at,
            )
            actual = (
                row.subscription_id,
                row.temporal_workflow_id,
                row.event_type,
                row.source_pattern,
                row.subject_pattern,
                row.correlation_key,
                row.output_schema_hash,
                row.expires_at,
            )
            if actual != expected:
                raise ControlPlaneConflict("event wait activation was reused with different data")

    async def close_event_wait(
        self,
        *,
        instance_id: str,
        node_id: str,
        activation: int,
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.scalar(
                select(EventWaitSubscription)
                .where(
                    EventWaitSubscription.instance_id == instance_id,
                    EventWaitSubscription.node_id == node_id,
                    EventWaitSubscription.activation == activation,
                )
                .with_for_update()
            )
            if row is None or row.status == "closed":
                return
            row.status = "closed"
            row.updated_at = await _database_now(session)

    async def advance_connector_checkpoint(
        self,
        *,
        connector_id: str,
        connector_kind: str,
        configuration_hash: str,
        checkpoint_value: str,
        expected_previous: str | None,
    ) -> ConnectorAdvance:
        if not connector_id or len(connector_id) > 64:
            raise ValueError("connector ID must be between 1 and 64 characters")
        if not checkpoint_value or len(checkpoint_value) > 2048:
            raise ValueError("connector checkpoint must be between 1 and 2048 characters")
        async with self._sessions() as session, session.begin():
            row = await session.scalar(
                select(ConnectorCheckpoint)
                .where(ConnectorCheckpoint.connector_id == connector_id)
                .with_for_update()
            )
            now = await _database_now(session)
            if row is None:
                if expected_previous is not None:
                    raise RevisionConflict("connector checkpoint no longer matches")
                session.add(
                    ConnectorCheckpoint(
                        connector_id=connector_id,
                        connector_kind=connector_kind,
                        configuration_hash=configuration_hash,
                        checkpoint_value=checkpoint_value,
                        revision=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return ConnectorAdvance(
                    initialized=True,
                    changed=False,
                    previous_value=None,
                    current_value=checkpoint_value,
                    revision=1,
                )
            if row.connector_kind != connector_kind or row.configuration_hash != configuration_hash:
                raise ControlPlaneConflict(
                    "connector ID was reused with a different immutable configuration"
                )
            if row.checkpoint_value != expected_previous:
                raise RevisionConflict("connector checkpoint no longer matches")
            previous = row.checkpoint_value
            if previous == checkpoint_value:
                return ConnectorAdvance(
                    initialized=False,
                    changed=False,
                    previous_value=previous,
                    current_value=checkpoint_value,
                    revision=row.revision,
                )
            row.checkpoint_value = checkpoint_value
            row.revision += 1
            row.updated_at = now
            return ConnectorAdvance(
                initialized=False,
                changed=True,
                previous_value=previous,
                current_value=checkpoint_value,
                revision=row.revision,
            )

    async def get_connector_checkpoint(
        self,
        connector_id: str,
    ) -> ConnectorCheckpointState | None:
        async with self._sessions() as session:
            row = await session.get(ConnectorCheckpoint, connector_id)
            if row is None:
                return None
            return ConnectorCheckpointState(
                connector_id=row.connector_id,
                connector_kind=row.connector_kind,
                configuration_hash=row.configuration_hash,
                checkpoint_value=row.checkpoint_value,
                revision=row.revision,
            )

    async def publish_projection(self, event: ProjectionEvent) -> bool:
        async with self._sessions() as session, session.begin():
            inserted = await session.scalar(
                pg_insert(WorkflowEvent)
                .values(
                    event_id=event.event_id,
                    instance_id=event.instance_id,
                    event_type=event.event_type,
                    data=event.data,
                    occurred_at=event.occurred_at,
                )
                .on_conflict_do_nothing(index_elements=[WorkflowEvent.event_id])
                .returning(WorkflowEvent.delivery_id)
            )
            if inserted is None:
                return False
            if event.event_type == "dev.misaka.workflow.snapshot.v1":
                await _apply_workflow_snapshot(session, event)
            return True

    async def claim_outbox(
        self,
        *,
        lease_owner: str,
        lease_duration: timedelta,
        limit: int = 20,
    ) -> tuple[OutboxCommand, ...]:
        if not lease_owner.strip():
            raise ValueError("outbox lease owner must not be blank")
        _validate_duration(lease_duration)
        _validate_limit(limit)
        async with self._sessions() as session, session.begin():
            now = await _database_now(session)
            rows = (
                await session.scalars(
                    select(CommandOutbox)
                    .where(
                        or_(
                            (
                                CommandOutbox.status.in_(("pending", "failed"))
                                & (CommandOutbox.available_at <= now)
                            ),
                            (
                                (CommandOutbox.status == "dispatching")
                                & (CommandOutbox.lease_expires_at <= now)
                            ),
                        )
                    )
                    .order_by(CommandOutbox.available_at, CommandOutbox.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            result: list[OutboxCommand] = []
            for row in rows:
                row.status = "dispatching"
                row.attempts += 1
                row.lease_owner = lease_owner
                row.lease_epoch += 1
                row.lease_expires_at = now + lease_duration
                row.updated_at = now
                result.append(_outbox_command(row))
            return tuple(result)

    async def complete_outbox(
        self,
        command: OutboxCommand,
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await _load_outbox_for_update(session, command.outbox_id)
            now = await _database_now(session)
            _require_outbox_fence(row, command, now)
            row.status = "dispatched"
            row.dispatched_at = now
            row.updated_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error = None

    async def renew_outbox(
        self,
        command: OutboxCommand,
        *,
        lease_duration: timedelta,
    ) -> bool:
        _validate_duration(lease_duration)
        async with self._sessions() as session, session.begin():
            row = await _load_outbox_for_update(session, command.outbox_id)
            now = await _database_now(session)
            try:
                _require_outbox_fence(row, command, now)
            except OutboxLeaseLost:
                return False
            row.lease_expires_at = now + lease_duration
            row.updated_at = now
            return True

    async def fail_outbox(
        self,
        command: OutboxCommand,
        *,
        error: str,
        retry_delay: timedelta,
        maximum_attempts: int,
    ) -> OutboxFailure:
        _validate_duration(retry_delay)
        if maximum_attempts < 1:
            raise ValueError("maximum outbox attempts must be positive")
        safe_error = error.strip()[:4096] or "outbox dispatch failed"
        async with self._sessions() as session, session.begin():
            row = await _load_outbox_for_update(session, command.outbox_id)
            now = await _database_now(session)
            _require_outbox_fence(row, command, now)
            row.last_error = safe_error
            row.updated_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            if row.attempts >= maximum_attempts:
                row.status = "dead"
                return OutboxFailure(status="dead", attempts=row.attempts, available_at=None)
            row.status = "failed"
            row.available_at = now + retry_delay
            return OutboxFailure(
                status="failed",
                attempts=row.attempts,
                available_at=row.available_at,
            )

    async def append_audit(
        self,
        *,
        audit_event_id: str,
        action: str,
        target_type: str,
        target_id: str,
        operator_label: str | None,
        reason: str | None,
        data: JsonObject,
    ) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                pg_insert(AuditLog)
                .values(
                    audit_event_id=audit_event_id,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    operator_label=operator_label,
                    reason=reason,
                    data=data,
                )
                .on_conflict_do_nothing(index_elements=[AuditLog.audit_event_id])
            )


async def _route_trigger(
    session: AsyncSession,
    *,
    trigger: TriggerDefinition,
    inbox: EventInbox,
    event: CloudEventEnvelope,
    event_document: JsonObject,
    now: datetime,
) -> str | None:
    delivery_id = str(
        uuid5(
            _EVENT_NAMESPACE,
            f"trigger.delivery\0{trigger.trigger_id}\0{inbox.inbox_id}",
        )
    )
    instance_id = str(
        uuid5(
            _EVENT_NAMESPACE,
            f"trigger.instance\0{trigger.trigger_id}\0{inbox.inbox_id}",
        )
    )
    try:
        workflow_input: JsonObject = {
            name: cast(JsonValue, jmespath.search(expression, event_document))
            for name, expression in cast(dict[str, str], trigger.input_bindings).items()
        }
        version = await _require_template_version(
            session,
            trigger.template_id,
            trigger.template_version,
        )
        plan = ExecutablePlan.model_validate(version.compiled_plan)
        input_schema = cast(JsonObject, json.loads(plan.input_schema.canonical))
        Draft202012Validator(input_schema).validate(workflow_input)  # pyright: ignore[reportUnknownMemberType]
    except (ValueError, json.JSONDecodeError, ValidationError) as exc:
        session.add(
            TriggerDelivery(
                delivery_id=delivery_id,
                trigger_id=trigger.trigger_id,
                inbox_id=inbox.inbox_id,
                status="failed",
                error_message=type(exc).__name__,
                created_at=now,
                updated_at=now,
            )
        )
        return None

    temporal_workflow_id = f"multi-agent-v2/instances/{instance_id}"
    session.add(
        WorkflowInstanceProjection(
            instance_id=instance_id,
            template_id=trigger.template_id,
            template_version=trigger.template_version,
            temporal_workflow_id=temporal_workflow_id,
            status="pending_start",
            workflow_input=workflow_input,
            trigger_cause={
                "kind": "cloud_event",
                "inboxId": inbox.inbox_id,
                "triggerId": trigger.trigger_id,
                "eventId": event.id,
                "source": event.source,
                "type": event.type,
            },
            projection_version=0,
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        TriggerDelivery(
            delivery_id=delivery_id,
            trigger_id=trigger.trigger_id,
            inbox_id=inbox.inbox_id,
            instance_id=instance_id,
            status="queued",
            created_at=now,
            updated_at=now,
        )
    )
    command_id = str(uuid5(_COMMAND_NAMESPACE, f"workflow.start.v1\0{instance_id}"))
    session.add(
        CommandOutbox(
            outbox_id=str(uuid4()),
            command_id=command_id,
            command_type="workflow.start.v1",
            aggregate_type="workflow_instance",
            aggregate_id=instance_id,
            payload={
                "instanceId": instance_id,
                "templateId": trigger.template_id,
                "templateVersion": trigger.template_version,
                "temporalWorkflowId": temporal_workflow_id,
                "workflowInput": workflow_input,
            },
            status="pending",
            attempts=0,
            available_at=now,
            lease_epoch=0,
            created_at=now,
            updated_at=now,
        )
    )
    return instance_id


async def _apply_workflow_snapshot(
    session: AsyncSession,
    event: ProjectionEvent,
) -> None:
    if event.instance_id is None:
        raise ControlPlaneConflict("workflow snapshot event requires an instance ID")
    snapshot = WorkflowSnapshotProjection.model_validate(event.data)
    instance = await session.scalar(
        select(WorkflowInstanceProjection)
        .where(WorkflowInstanceProjection.instance_id == event.instance_id)
        .with_for_update()
    )
    if instance is None:
        raise ControlPlaneNotFound("workflow projection placeholder does not exist")
    if instance.temporal_workflow_id != snapshot.temporal_workflow_id:
        raise ControlPlaneConflict("workflow snapshot Temporal identity does not match")
    if snapshot.projection_version <= instance.projection_version:
        return
    now = await _database_now(session)
    waiting = any(node.status in {"waiting_approval", "waiting_event"} for node in snapshot.nodes)
    projected_status = "waiting" if snapshot.status == "running" and waiting else snapshot.status
    instance.temporal_run_id = snapshot.temporal_run_id or instance.temporal_run_id
    instance.status = projected_status
    instance.output = snapshot.output
    instance.error_code = snapshot.error.code if snapshot.error else None
    instance.error_message = snapshot.error.message if snapshot.error else None
    instance.projection_version = snapshot.projection_version
    instance.updated_at = now
    if instance.started_at is None and snapshot.projection_version > 0:
        instance.started_at = event.occurred_at
    if snapshot.status in {"succeeded", "failed", "cancelled", "attention_required"}:
        instance.completed_at = event.occurred_at

    for node in snapshot.nodes:
        node_key = {
            "instance_id": event.instance_id,
            "node_id": node.node_id,
            "activation": node.activation,
        }
        node_row = await session.get(WorkflowNodeProjection, node_key)
        if node_row is None:
            node_row = WorkflowNodeProjection(
                **node_key,
                execution_id=node.execution_id,
                status=node.status,
                output=node.output,
                error_code=node.error.code if node.error else None,
                error_message=node.error.message if node.error else None,
                projection_version=snapshot.projection_version,
                updated_at=now,
            )
            session.add(node_row)
        elif node_row.projection_version < snapshot.projection_version:
            node_row.execution_id = node.execution_id
            node_row.status = node.status
            node_row.output = node.output
            node_row.error_code = node.error.code if node.error else None
            node_row.error_message = node.error.message if node.error else None
            node_row.projection_version = snapshot.projection_version
            node_row.updated_at = now
        await _project_approval(
            session,
            instance_id=event.instance_id,
            node=node,
            occurred_at=event.occurred_at,
            now=now,
        )


async def _project_approval(
    session: AsyncSession,
    *,
    instance_id: str,
    node: ProjectionNodeState,
    occurred_at: datetime,
    now: datetime,
) -> None:
    approval = await session.scalar(
        select(ApprovalProjection)
        .where(
            ApprovalProjection.instance_id == instance_id,
            ApprovalProjection.node_id == node.node_id,
            ApprovalProjection.activation == node.activation,
        )
        .with_for_update()
    )
    if node.status == "waiting_approval":
        if approval is not None:
            return
        approval_id = str(
            uuid5(
                _EVENT_NAMESPACE,
                f"approval\0{instance_id}\0{node.node_id}\0{node.activation}",
            )
        )
        session.add(
            ApprovalProjection(
                approval_id=approval_id,
                instance_id=instance_id,
                node_id=node.node_id,
                activation=node.activation,
                label=node.approval_label or node.node_id,
                status="pending",
                requested_at=occurred_at,
            )
        )
        await _append_audit_row(
            session,
            audit_event_id=str(uuid5(_EVENT_NAMESPACE, f"approval.requested\0{approval_id}")),
            action="approval.requested",
            target_type="approval",
            target_id=approval_id,
            operator_label=None,
            reason=None,
            data={"instanceId": instance_id, "nodeId": node.node_id},
            now=now,
        )
        return
    if approval is None or approval.status != "pending":
        return
    output = node.output or {}
    decision = output.get("decision")
    if node.status == "succeeded" and decision in {"approved", "rejected"}:
        approval.status = decision
        command_id = output.get("commandId")
        approval.command_id = command_id if isinstance(command_id, str) else None
        operator_label = output.get("operatorLabel")
        reason = output.get("reason")
        approval.operator_label = operator_label if isinstance(operator_label, str) else None
        approval.reason = reason if isinstance(reason, str) else None
    elif node.status == "timed_out":
        approval.status = "timed_out"
    elif node.status in {
        "cancelled",
        "failed",
        "skipped",
        "reconciliation_required",
    }:
        approval.status = "cancelled"
    else:
        return
    approval.decided_at = occurred_at
    await _append_audit_row(
        session,
        audit_event_id=str(
            uuid5(
                _EVENT_NAMESPACE,
                f"approval.decided\0{approval.approval_id}\0{approval.status}",
            )
        ),
        action="approval.decided",
        target_type="approval",
        target_id=approval.approval_id,
        operator_label=approval.operator_label,
        reason=approval.reason,
        data={"status": approval.status, "commandId": approval.command_id},
        now=now,
    )


async def _append_audit_row(
    session: AsyncSession,
    *,
    audit_event_id: str,
    action: str,
    target_type: str,
    target_id: str,
    operator_label: str | None,
    reason: str | None,
    data: JsonObject,
    now: datetime,
) -> None:
    await session.execute(
        pg_insert(AuditLog)
        .values(
            audit_event_id=audit_event_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            operator_label=operator_label,
            reason=reason,
            data=data,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[AuditLog.audit_event_id])
    )


async def _existing_ingest_result(
    session: AsyncSession,
    source: str,
    event_id: str,
) -> EventIngestResult:
    inbox = await session.scalar(
        select(EventInbox).where(
            EventInbox.source == source,
            EventInbox.event_id == event_id,
        )
    )
    if inbox is None:
        raise ControlPlaneRepositoryError("duplicate event inbox row was not visible")
    routed = (
        await session.scalars(
            select(TriggerDelivery.instance_id).where(
                TriggerDelivery.inbox_id == inbox.inbox_id,
                TriggerDelivery.instance_id.is_not(None),
            )
        )
    ).all()
    signalled = (
        await session.scalars(
            select(EventWaitSubscription.temporal_workflow_id).where(
                EventWaitSubscription.delivered_inbox_id == inbox.inbox_id
            )
        )
    ).all()
    return EventIngestResult(
        inbox_id=inbox.inbox_id,
        duplicate=True,
        routed_instances=tuple(sorted(cast(Sequence[str], routed))),
        signalled_workflows=tuple(sorted(signalled)),
    )


def _event_document(event: CloudEventEnvelope) -> JsonObject:
    document = cast(
        JsonObject,
        event.model_dump(
            mode="json",
            by_alias=True,
            exclude={"extensions"},
        ),
    )
    document["extensions"] = event.extensions
    return document


def _event_correlation_key(event: CloudEventEnvelope) -> str | None:
    extension_value = event.extensions.get("correlationkey")
    if isinstance(extension_value, str):
        return extension_value
    for name in ("correlationKey", "correlation_key"):
        value = event.data.get(name)
        if isinstance(value, str):
            return value
    return None


def _pattern_matches(pattern: str | None, value: str | None) -> bool:
    if pattern is None:
        return True
    return value is not None and fnmatchcase(value, pattern)


def _workflow_schedule_target(target: JsonObject) -> tuple[str, int, JsonObject]:
    template_id = target.get("templateId")
    template_version = target.get("templateVersion")
    workflow_input = target.get("workflowInput", {})
    if (
        not isinstance(template_id, str)
        or not template_id
        or isinstance(template_version, bool)
        or not isinstance(template_version, int)
        or template_version < 1
        or not isinstance(workflow_input, dict)
    ):
        raise ControlPlaneConflict("workflow schedule target is invalid")
    return template_id, template_version, cast(JsonObject, workflow_input)


async def _claim_idempotency(
    session: AsyncSession,
    scope: str,
    key: str,
    request_hash: str,
) -> Mapping[str, object] | None:
    await session.execute(
        pg_insert(IdempotencyRecord)
        .values(
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            response={},
        )
        .on_conflict_do_nothing(
            index_elements=[IdempotencyRecord.scope, IdempotencyRecord.idempotency_key]
        )
    )
    row = await session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
        )
        .with_for_update()
    )
    if row is None:
        raise ControlPlaneRepositoryError("idempotency record was not visible after insertion")
    if row.request_hash != request_hash:
        raise IdempotencyConflict("idempotency key was reused with a different request")
    if row.response:
        return cast(Mapping[str, object], row.response)
    return None


async def _complete_idempotency(
    session: AsyncSession,
    scope: str,
    key: str,
    response: BaseModel,
) -> None:
    row = await session.get(
        IdempotencyRecord,
        {"scope": scope, "idempotency_key": key},
    )
    if row is None:
        raise ControlPlaneRepositoryError("idempotency record disappeared before completion")
    row.response = response.model_dump(mode="json", by_alias=True)
    row.updated_at = await _database_now(session)


async def _require_template_version(
    session: AsyncSession,
    template_id: str,
    template_version: int,
) -> WorkflowTemplateVersion:
    row = await session.get(
        WorkflowTemplateVersion,
        {"template_id": template_id, "version": template_version},
    )
    if row is None:
        raise ControlPlaneNotFound("workflow template version does not exist")
    return row


async def _append_schedule_sync_command(
    session: AsyncSession,
    row: ScheduleDefinition,
    now: datetime,
) -> None:
    command_id = str(
        uuid5(
            _COMMAND_NAMESPACE,
            f"schedule.sync.v1\0{row.schedule_id}\0{row.revision}",
        )
    )
    session.add(
        CommandOutbox(
            outbox_id=str(uuid4()),
            command_id=command_id,
            command_type="schedule.sync.v1",
            aggregate_type="schedule",
            aggregate_id=row.schedule_id,
            payload=_schedule_record(row).model_dump(mode="json", by_alias=True),
            status="pending",
            attempts=0,
            available_at=now,
            lease_epoch=0,
            created_at=now,
            updated_at=now,
        )
    )


async def _load_outbox_for_update(session: AsyncSession, outbox_id: str) -> CommandOutbox:
    row = await session.scalar(
        select(CommandOutbox).where(CommandOutbox.outbox_id == outbox_id).with_for_update()
    )
    if row is None:
        raise ControlPlaneNotFound("outbox command does not exist")
    return row


def _require_outbox_fence(
    row: CommandOutbox,
    command: OutboxCommand,
    now: datetime,
) -> None:
    if (
        row.status != "dispatching"
        or row.lease_owner != command.lease_owner
        or row.lease_epoch != command.lease_epoch
        or row.lease_expires_at is None
        or row.lease_expires_at <= now
    ):
        raise OutboxLeaseLost("outbox command lease is no longer owned by this dispatcher")


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if value is None:
        raise ControlPlaneRepositoryError("PostgreSQL did not return a current timestamp")
    return value


def _request_hash(value: BaseModel | object) -> str:
    if isinstance(value, BaseModel):
        document = value.model_dump(mode="json", by_alias=True)
    else:
        document = value
    return sha256_text(canonical_json(cast(JsonValue, document)))


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("Idempotency-Key must be between 1 and 128 characters")
    return normalized


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("query limit must be between 1 and 1000")


def _validate_duration(value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError("duration must be positive")


def _template_record(row: WorkflowTemplate) -> TemplateRecord:
    return TemplateRecord(
        template_id=row.template_id,
        name=row.name,
        description=row.description,
        latest_version=row.latest_version,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _template_version_record(row: WorkflowTemplateVersion) -> TemplateVersionRecord:
    return TemplateVersionRecord(
        template_id=row.template_id,
        version=row.version,
        definition=cast(JsonObject, row.definition),
        compiled_plan=ExecutablePlan.model_validate(row.compiled_plan),
        plan_hash=row.plan_hash,
        catalog_revision=row.catalog_revision,
        created_at=row.created_at,
    )


def _provider_catalog_record(row: ProviderCatalogSnapshot) -> ProviderCatalogRecord:
    return ProviderCatalogRecord(
        runtime_name=row.runtime_name,
        runtime_id=row.runtime_id,
        provider_id=row.provider_id,
        revision=row.revision,
        models=tuple(CatalogModelRecord.model_validate(model) for model in row.models),
        updated_at=row.updated_at,
    )


def _instance_record(row: WorkflowInstanceProjection) -> InstanceRecord:
    return InstanceRecord(
        instance_id=row.instance_id,
        template_id=row.template_id,
        template_version=row.template_version,
        temporal_workflow_id=row.temporal_workflow_id,
        temporal_run_id=row.temporal_run_id,
        status=cast(Any, row.status),
        workflow_input=cast(JsonObject, row.workflow_input),
        output=cast(JsonObject | None, row.output),
        error_code=row.error_code,
        error_message=row.error_message,
        trigger_cause=cast(JsonObject | None, row.trigger_cause),
        projection_version=row.projection_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _node_projection_record(row: WorkflowNodeProjection) -> NodeProjectionRecord:
    return NodeProjectionRecord(
        instance_id=row.instance_id,
        node_id=row.node_id,
        activation=row.activation,
        execution_id=row.execution_id,
        status=row.status,
        output=cast(JsonObject | None, row.output),
        error_code=row.error_code,
        error_message=row.error_message,
        projection_version=row.projection_version,
        updated_at=row.updated_at,
    )


def _approval_record(row: ApprovalProjection) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.approval_id,
        instance_id=row.instance_id,
        node_id=row.node_id,
        activation=row.activation,
        label=row.label,
        status=cast(Any, row.status),
        command_id=row.command_id,
        operator_label=row.operator_label,
        reason=row.reason,
        requested_at=row.requested_at,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
    )


def _workflow_event_record(row: WorkflowEvent) -> WorkflowEventRecord:
    return WorkflowEventRecord(
        delivery_id=row.delivery_id,
        event_id=row.event_id,
        instance_id=row.instance_id,
        event_type=row.event_type,
        data=cast(JsonObject, row.data),
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


def _trigger_record(row: TriggerDefinition) -> TriggerRecord:
    return TriggerRecord(
        trigger_id=row.trigger_id,
        name=row.name,
        revision=row.revision,
        enabled=row.enabled,
        event_type=row.event_type,
        source_pattern=row.source_pattern,
        subject_pattern=row.subject_pattern,
        template_id=row.template_id,
        template_version=row.template_version,
        input_bindings=cast(dict[str, str], row.input_bindings),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _schedule_record(row: ScheduleDefinition) -> ScheduleRecord:
    return ScheduleRecord(
        schedule_id=row.schedule_id,
        name=row.name,
        revision=row.revision,
        enabled=row.enabled,
        schedule_kind=cast(Any, row.schedule_kind),
        schedule_spec=cast(JsonObject, row.schedule_spec),
        target_kind=cast(Any, row.target_kind),
        target=cast(JsonObject, row.target),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _outbox_command(row: CommandOutbox) -> OutboxCommand:
    if row.lease_owner is None:
        raise ControlPlaneRepositoryError("claimed outbox command has no lease owner")
    return OutboxCommand(
        outbox_id=row.outbox_id,
        command_id=row.command_id,
        command_type=row.command_type,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        payload=cast(JsonObject, row.payload),
        attempts=row.attempts,
        lease_owner=row.lease_owner,
        lease_epoch=row.lease_epoch,
    )
