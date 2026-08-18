from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import DurableConflict, DurableJobStatus, DurableNotFound
from misaka_persistence_jsonl import JsonlEventLog

from misaka_control_plane.models import TemplateSubmission


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    definition: TemplateSubmission
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InstanceRecord:
    instance_id: str
    idempotency_key: str
    template_id: str
    template_version: int
    input: JsonObject
    status: DurableJobStatus
    version: int = 1
    result: JsonObject | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class JsonlTemplateRegistry:
    """Durable template versions and instance facts for the local profile."""

    _TEMPLATE_STREAM = "control.templates"
    _INSTANCE_STREAM = "control.instances"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._templates: dict[tuple[str, int], TemplateRecord] = {}
        self._instances: dict[str, InstanceRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._loaded = False

    async def open(self) -> None:
        if self._loaded:
            return
        for event in await self._log.read(self._TEMPLATE_STREAM):
            if event.event_type != "template.created":
                raise DurableConflict("control.unknown_template_event", event.event_type)
            definition = _decode_template(event.payload.get("definition"))
            created_at = datetime.fromisoformat(str(event.payload["created_at"]))
            self._templates[(definition.template_id, definition.version)] = TemplateRecord(
                definition, created_at
            )
        for event in await self._log.read(self._INSTANCE_STREAM):
            if event.event_type not in {"instance.created", "instance.updated"}:
                raise DurableConflict("control.unknown_instance_event", event.event_type)
            instance = _decode_instance(event.payload)
            self._instances[instance.instance_id] = instance
            self._idempotency[instance.idempotency_key] = instance.instance_id
        self._loaded = True

    async def create_template(self, definition: TemplateSubmission) -> TemplateRecord:
        await self.open()
        _validate_template(definition)
        key = (definition.template_id, definition.version)
        existing = self._templates.get(key)
        if existing is not None:
            if existing.definition.model_dump(mode="json") != definition.model_dump(mode="json"):
                raise DurableConflict("control.template_version_conflict", str(key))
            return existing
        created_at = datetime.now(UTC)
        await self._log.append(
            self._TEMPLATE_STREAM,
            f"template-created:{definition.template_id}:{definition.version}",
            "template.created",
            {
                "definition": definition.model_dump(mode="json"),
                "created_at": created_at.isoformat(),
            },
        )
        record = TemplateRecord(definition, created_at)
        self._templates[key] = record
        return record

    async def get_template(self, template_id: str, version: int | None = None) -> TemplateRecord:
        await self.open()
        if version is not None:
            record = self._templates.get((template_id, version))
            if record is None:
                raise DurableNotFound("control.template_not_found", str((template_id, version)))
            return record
        candidates = [
            record
            for (candidate_id, _), record in self._templates.items()
            if candidate_id == template_id
        ]
        if not candidates:
            raise DurableNotFound("control.template_not_found", template_id)
        return max(candidates, key=lambda record: record.definition.version)

    async def list_templates(self) -> tuple[TemplateRecord, ...]:
        await self.open()
        return tuple(
            sorted(
                self._templates.values(),
                key=lambda record: (record.definition.template_id, record.definition.version),
            )
        )

    async def create_instance(
        self,
        instance_id: str,
        idempotency_key: str,
        template_id: str,
        template_version: int,
        input: JsonObject,
    ) -> tuple[InstanceRecord, bool]:
        await self.open()
        await self.get_template(template_id, template_version)
        existing_id = self._idempotency.get(idempotency_key)
        if existing_id is not None:
            existing = self._instances[existing_id]
            if existing.instance_id != instance_id or existing.input != input:
                raise DurableConflict("control.instance_idempotency_conflict", idempotency_key)
            return existing, False
        existing = self._instances.get(instance_id)
        if existing is not None:
            if existing.idempotency_key != idempotency_key or existing.input != input:
                raise DurableConflict("control.instance_conflict", instance_id)
            return existing, False
        now = datetime.now(UTC)
        instance = InstanceRecord(
            instance_id=instance_id,
            idempotency_key=idempotency_key,
            template_id=template_id,
            template_version=template_version,
            input=input,
            status=DurableJobStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        await self._log.append(
            self._INSTANCE_STREAM,
            f"instance-created:{instance_id}",
            "instance.created",
            _encode_instance(instance),
        )
        self._instances[instance_id] = instance
        self._idempotency[idempotency_key] = instance_id
        return instance, True

    async def get_instance(self, instance_id: str) -> InstanceRecord:
        await self.open()
        instance = self._instances.get(instance_id)
        if instance is None:
            raise DurableNotFound("control.instance_not_found", instance_id)
        return instance

    async def list_instances(self) -> tuple[InstanceRecord, ...]:
        await self.open()
        return tuple(self._instances.values())

    async def transition_instance(
        self,
        instance_id: str,
        status: DurableJobStatus,
        *,
        expected_version: int | None = None,
        result: JsonObject | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> InstanceRecord:
        current = await self.get_instance(instance_id)
        if expected_version is not None and current.version != expected_version:
            raise DurableConflict("control.instance_version_conflict", instance_id)
        terminal = {
            DurableJobStatus.SUCCEEDED,
            DurableJobStatus.FAILED,
            DurableJobStatus.CANCELLED,
            DurableJobStatus.RECONCILIATION_REQUIRED,
        }
        if current.status in terminal:
            if current.status is status and current.result == result:
                return current
            raise DurableConflict("control.instance_terminal_conflict", instance_id)
        updated = InstanceRecord(
            instance_id=current.instance_id,
            idempotency_key=current.idempotency_key,
            template_id=current.template_id,
            template_version=current.template_version,
            input=current.input,
            status=status,
            version=current.version + 1,
            result=result,
            error_code=error_code,
            error_message=error_message,
            created_at=current.created_at,
            updated_at=datetime.now(UTC),
        )
        await self._log.append(
            self._INSTANCE_STREAM,
            f"instance-updated:{instance_id}:{updated.version}",
            "instance.updated",
            _encode_instance(updated),
        )
        self._instances[instance_id] = updated
        return updated


def _validate_template(definition: TemplateSubmission) -> None:
    nodes = {node.node_id: node for node in definition.nodes}
    if len(nodes) != len(definition.nodes):
        raise DurableConflict("control.template_node_duplicate", definition.template_id)
    if definition.coordinator == "direct" and len(definition.nodes) != 1:
        raise DurableConflict("control.direct_template_shape", definition.template_id)
    for node in definition.nodes:
        missing = set(node.depends_on) - set(nodes)
        if missing:
            raise DurableConflict("control.template_dependency_missing", str(sorted(missing)))
    pending = {node_id: set(node.depends_on) for node_id, node in nodes.items()}
    while pending:
        ready = {node_id for node_id, deps in pending.items() if not deps}
        if not ready:
            raise DurableConflict("control.template_cycle", definition.template_id)
        for node_id in ready:
            pending.pop(node_id)
        for deps in pending.values():
            deps.difference_update(ready)


def _encode_instance(instance: InstanceRecord) -> JsonObject:
    return {
        "instance_id": instance.instance_id,
        "idempotency_key": instance.idempotency_key,
        "template_id": instance.template_id,
        "template_version": instance.template_version,
        "input": instance.input,
        "status": instance.status.value,
        "version": instance.version,
        "result": instance.result,
        "error_code": instance.error_code,
        "error_message": instance.error_message,
        "created_at": instance.created_at.isoformat(),
        "updated_at": instance.updated_at.isoformat(),
    }


def _decode_template(value: object) -> TemplateSubmission:
    if not isinstance(value, dict):
        raise ValueError("template definition must be an object")
    return TemplateSubmission.model_validate(value)


def _decode_instance(value: JsonObject) -> InstanceRecord:
    input_value = value.get("input", {})
    result = value.get("result")
    if not isinstance(input_value, dict) or (result is not None and not isinstance(result, dict)):
        raise ValueError("instance input/result must be objects")
    return InstanceRecord(
        instance_id=str(value["instance_id"]),
        idempotency_key=str(value["idempotency_key"]),
        template_id=str(value["template_id"]),
        template_version=_as_int(value["template_version"]),
        input=cast(JsonObject, input_value),
        status=DurableJobStatus(str(value["status"])),
        version=_as_int(value["version"]),
        result=cast(JsonObject, result) if result is not None else None,
        error_code=_optional_string(value.get("error_code")),
        error_message=_optional_string(value.get("error_message")),
        created_at=datetime.fromisoformat(str(value["created_at"])),
        updated_at=datetime.fromisoformat(str(value["updated_at"])),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("integer value must not be boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError("integer value is invalid")
