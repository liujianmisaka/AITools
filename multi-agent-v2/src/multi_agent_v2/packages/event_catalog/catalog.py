from __future__ import annotations

import json
from typing import Literal, cast

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.event_catalog.models import EventDescriptor

_OBJECT_SCHEMA = cast(
    JsonObject,
    {
        "type": "object",
        "additionalProperties": False,
    },
)
_COMMAND_SCHEMA = cast(
    JsonObject,
    {
        "type": "object",
        "required": ["commandId"],
        "properties": {"commandId": {"type": "string"}},
        "additionalProperties": True,
    },
)
_CLOUD_EVENT_SCHEMA = cast(
    JsonObject,
    {
        "type": "object",
        "required": ["specversion", "id", "source", "type", "data"],
        "properties": {
            "specversion": {"const": "1.0"},
            "id": {"type": "string"},
            "source": {"type": "string"},
            "type": {"type": "string"},
            "data": {"type": "object"},
        },
        "additionalProperties": True,
    },
)
_EVIDENCE_SCHEMA = cast(
    JsonObject,
    {
        "type": "object",
        "required": ["executionId", "sequence", "eventType", "payload"],
        "properties": {
            "executionId": {"type": "string"},
            "sequence": {"type": "integer", "minimum": 1},
            "eventType": {"type": "string"},
            "payload": {"type": "object"},
        },
        "additionalProperties": False,
    },
)


def _descriptor(
    event_name: str,
    *,
    producer: str,
    consumers: tuple[str, ...],
    transport: str,
    source_of_truth: str,
    deduplication_key: str,
    ordering: str,
    persistent: bool,
    temporal_history: bool,
    payload_schema: JsonObject,
    redaction: tuple[str, ...] = (),
    status: Literal["implemented", "contract_only"] = "implemented",
) -> EventDescriptor:
    return EventDescriptor.model_validate(
        {
            "event_name": event_name,
            "version": 1,
            "status": status,
            "producer": producer,
            "consumers": consumers,
            "transport": transport,
            "source_of_truth": source_of_truth,
            "deduplication_key": deduplication_key,
            "ordering": ordering,
            "persistent": persistent,
            "temporal_history": temporal_history,
            "payload_schema": payload_schema,
            "redaction": redaction,
        }
    )


_WORKFLOW_COMMANDS = (
    _descriptor(
        "approval.decide.v1",
        producer="Control API",
        consumers=("DurableWorkflow",),
        transport="Temporal Update",
        source_of_truth="Temporal Workflow History",
        deduplication_key="commandId",
        ordering="Serialized by Workflow Task for one workflow instance",
        persistent=True,
        temporal_history=True,
        payload_schema=_COMMAND_SCHEMA,
    ),
    _descriptor(
        "approval.submit.v1",
        producer="Command Dispatcher",
        consumers=("DurableWorkflow",),
        transport="Temporal Signal",
        source_of_truth="Temporal Workflow History",
        deduplication_key="commandId and node activation",
        ordering="Serialized by Workflow Task for one workflow instance",
        persistent=True,
        temporal_history=True,
        payload_schema=_COMMAND_SCHEMA,
    ),
    _descriptor(
        "event.deliver.v1",
        producer="Command Dispatcher",
        consumers=("DurableWorkflow",),
        transport="Temporal Signal",
        source_of_truth="Temporal Workflow History",
        deduplication_key="CloudEvent source and id",
        ordering="Inbox delivery order per workflow instance",
        persistent=True,
        temporal_history=True,
        payload_schema=_COMMAND_SCHEMA,
    ),
    _descriptor(
        "human.answer.v1",
        producer="Human interaction Control API bridge",
        consumers=("DurableWorkflow",),
        transport="Temporal Update or Signal",
        source_of_truth="Temporal Workflow History after acceptance",
        deduplication_key="commandId and question batch activation",
        ordering="Serialized by Workflow Task for one workflow instance",
        persistent=True,
        temporal_history=True,
        payload_schema=_COMMAND_SCHEMA,
        status="contract_only",
    ),
)

_CONTROL_EVENTS = (
    _descriptor(
        "workflow.start.v1",
        producer="ControlPlaneService",
        consumers=("Command Dispatcher", "Temporal Client"),
        transport="PostgreSQL transactional Outbox",
        source_of_truth="command_outbox",
        deduplication_key="commandId",
        ordering="Outbox creation order; idempotent Temporal workflow ID",
        persistent=True,
        temporal_history=False,
        payload_schema=_COMMAND_SCHEMA,
    ),
    _descriptor(
        "workflow.signal.v1",
        producer="ControlPlaneService",
        consumers=("Command Dispatcher", "Temporal Workflow"),
        transport="PostgreSQL transactional Outbox then Temporal Signal",
        source_of_truth="command_outbox until delivered; then Temporal History",
        deduplication_key="commandId",
        ordering="Outbox creation order per target workflow",
        persistent=True,
        temporal_history=True,
        payload_schema=_COMMAND_SCHEMA,
    ),
    _descriptor(
        "schedule.sync.v1",
        producer="ControlPlaneService",
        consumers=("Command Dispatcher", "Temporal Schedule API"),
        transport="PostgreSQL transactional Outbox",
        source_of_truth="schedule_definition and command_outbox",
        deduplication_key="schedule ID and revision",
        ordering="Revision order per schedule",
        persistent=True,
        temporal_history=False,
        payload_schema=_COMMAND_SCHEMA,
    ),
    _descriptor(
        "dev.misaka.workflow.snapshot.v1",
        producer="DurableWorkflow",
        consumers=("projection.publish.v1 Activity", "Control API", "Web BFF"),
        transport="Temporal Activity to PostgreSQL projection",
        source_of_truth="Temporal Workflow state; PostgreSQL is a query projection",
        deduplication_key="workflow instance ID and projection version",
        ordering="Strictly increasing projection version per workflow instance",
        persistent=True,
        temporal_history=True,
        payload_schema=_OBJECT_SCHEMA,
        redaction=("Agent prompts and credential values are forbidden",),
    ),
)

_EXTERNAL_EVENTS = (
    _descriptor(
        "dev.misaka.webhook.received.v1",
        producer="Generic Webhook ingress",
        consumers=("event_inbox", "Trigger dispatcher", "waiting Workflows"),
        transport="CloudEvents HTTP to PostgreSQL Inbox",
        source_of_truth="event_inbox",
        deduplication_key="source and CloudEvent id",
        ordering="No cross-source ordering; Inbox delivery ID orders local ingestion",
        persistent=True,
        temporal_history=False,
        payload_schema=_CLOUD_EVENT_SCHEMA,
        redaction=("Credential-like payload keys must be redacted before projection",),
    ),
    _descriptor(
        "dev.misaka.git.commit.updated.v1",
        producer="GitRefPoller",
        consumers=("event_inbox", "Trigger dispatcher", "waiting Workflows"),
        transport="Local connector to PostgreSQL Inbox",
        source_of_truth="connector_checkpoint and event_inbox",
        deduplication_key="repository, ref and observed commit",
        ordering="Monotonic checkpoint per connector",
        persistent=True,
        temporal_history=False,
        payload_schema=_CLOUD_EVENT_SCHEMA,
    ),
)

_EVIDENCE_NAMES = (
    "execution_registered",
    "lease_acquired",
    "attempt_started",
    "workspace_prepared",
    "session_prepare_intended",
    "session_prepared",
    "turn_start_intended",
    "turn_started",
    "provider_event_observed",
    "provider_terminal_observed",
    "output_validated",
    "cancellation_requested",
    "reconciliation_required",
    "artifact_committed",
    "cleanup_started",
    "cleanup_completed",
    "cleanup_preserved",
    "cleanup_failed",
)
_EXECUTION_EVIDENCE = tuple(
    _descriptor(
        event_name,
        producer="AgentActivityRunner",
        consumers=("Execution evidence query", "Web BFF", "Reconciler"),
        transport="Append-only PostgreSQL execution evidence",
        source_of_truth="Observed fact only; never Workflow state",
        deduplication_key="execution ID and evidence sequence",
        ordering="Strictly increasing sequence per execution",
        persistent=True,
        temporal_history=False,
        payload_schema=_EVIDENCE_SCHEMA,
        redaction=(
            "Credential-like keys are removed recursively",
            "Token deltas and raw environment values are forbidden",
        ),
    )
    for event_name in _EVIDENCE_NAMES
)
_EXECUTION_EVIDENCE_CONTRACTS = (
    _descriptor(
        "cancellation_confirmed",
        producer="Agent Runtime adapter",
        consumers=("Execution evidence query", "Web BFF", "Reconciler"),
        transport="Append-only PostgreSQL execution evidence",
        source_of_truth="Observed provider or process termination fact",
        deduplication_key="execution ID and cancellation observation identity",
        ordering="After cancellation_requested for the same execution",
        persistent=True,
        temporal_history=False,
        payload_schema=_EVIDENCE_SCHEMA,
        redaction=("Raw process output and credential values are forbidden",),
        status="contract_only",
    ),
)

EVENT_CATALOG = tuple(
    sorted(
        (
            *_WORKFLOW_COMMANDS,
            *_CONTROL_EVENTS,
            *_EXTERNAL_EVENTS,
            *_EXECUTION_EVIDENCE,
            *_EXECUTION_EVIDENCE_CONTRACTS,
        ),
        key=lambda descriptor: descriptor.event_name,
    )
)


def render_catalog_json() -> str:
    schemas = {
        "cloudEvent": _CLOUD_EVENT_SCHEMA,
        "command": _COMMAND_SCHEMA,
        "evidence": _EVIDENCE_SCHEMA,
        "object": _OBJECT_SCHEMA,
    }
    document = {
        "catalogVersion": 1,
        "payloadSchemas": schemas,
        "events": [_document_entry(descriptor, schemas) for descriptor in EVENT_CATALOG],
    }
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _document_entry(
    descriptor: EventDescriptor,
    schemas: dict[str, JsonObject],
) -> dict[str, object]:
    entry = descriptor.model_dump(mode="json", by_alias=True, exclude={"payload_schema"})
    matching = next(name for name, schema in schemas.items() if schema == descriptor.payload_schema)
    entry["payload_schema_ref"] = matching
    return entry
