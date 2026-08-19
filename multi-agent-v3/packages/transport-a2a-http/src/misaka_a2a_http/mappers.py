from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import cast

from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatusUpdateEvent,
)
from a2a.types.a2a_pb2 import (
    TaskStatus as ProtoTaskStatus,
)
from google.protobuf.json_format import MessageToDict, Parse, ParseDict
from google.protobuf.struct_pb2 import Struct, Value
from google.protobuf.timestamp_pb2 import Timestamp
from misaka_a2a_capability import (
    A2AAgentCard,
    TaskEvent,
    TaskRequest,
    TaskSnapshot,
    TaskStatus,
)
from misaka_invocation_contracts import CapabilityFeature, SessionRef
from misaka_kernel_contracts import JsonObject, JsonValue

CAPABILITY_EXTENSION_URI = "urn:misaka:a2a:capability-contract:v1"


def task_request_to_proto(
    request: TaskRequest,
    *,
    return_immediately: bool = False,
) -> SendMessageRequest:
    metadata: JsonObject = dict(request.metadata)
    metadata.update(
        {
            "capabilityId": request.capability_id,
            "operation": request.operation,
            "idempotencyKey": request.idempotency_key,
            "requiredFeatures": [
                feature.value
                for feature in sorted(request.required_features, key=lambda value: value.value)
            ],
            "policyContext": request.policy_context,
        }
    )
    if request.provider_id is not None:
        metadata["providerId"] = request.provider_id
    if request.model is not None:
        metadata["model"] = request.model
    if request.effort is not None:
        metadata["effort"] = request.effort
    if request.output_schema is not None:
        metadata["outputSchema"] = request.output_schema
    if request.session_ref is not None:
        metadata["sessionRef"] = {
            "provider": request.session_ref.provider,
            "nativeId": request.session_ref.native_id,
        }
    return SendMessageRequest(
        message=Message(
            message_id=request.message_id,
            context_id=request.context_id,
            task_id=request.task_id,
            role=Role.ROLE_USER,
            parts=[Part(data=_value(request.input), media_type="application/json")],
            metadata=_struct(metadata),
        ),
        configuration=SendMessageConfiguration(return_immediately=return_immediately),
    )


def task_request_from_proto(params: SendMessageRequest) -> TaskRequest:
    message = params.message
    metadata = {
        **_struct_to_object(params.metadata),
        **_struct_to_object(message.metadata),
    }
    capability_id = _required_string(metadata, "capabilityId")
    operation = _required_string(metadata, "operation")
    provider_id = _optional_string(metadata, "providerId")
    model = _optional_string(metadata, "model")
    effort = _optional_string(metadata, "effort")
    session_ref = _session_ref(metadata.get("sessionRef"))
    required_features = _features(metadata.get("requiredFeatures"))
    output_schema = _optional_object(metadata.get("outputSchema"), "outputSchema")
    policy_context = _optional_object(metadata.get("policyContext"), "policyContext") or {}

    message_id = message.message_id.strip()
    if not message_id:
        raise ValueError("A2A message.messageId must not be empty")
    task_id = message.task_id.strip() or f"task-{uuid.uuid4()}"
    context_id = message.context_id.strip() or f"context-{uuid.uuid4()}"
    idempotency_key = _optional_string(metadata, "idempotencyKey") or message_id

    return TaskRequest(
        task_id=task_id,
        context_id=context_id,
        message_id=message_id,
        idempotency_key=idempotency_key,
        capability_id=capability_id,
        operation=operation,
        input=_input_from_parts(message.parts),
        provider_id=provider_id,
        model=model,
        effort=effort,
        session_ref=session_ref,
        required_features=required_features,
        output_schema=output_schema,
        policy_context=policy_context,
        metadata=metadata,
    )


def agent_card_to_proto(
    card: A2AAgentCard,
    *,
    interface_url: str,
) -> AgentCard:
    skills_payload: list[JsonValue] = []
    for skill in card.skills:
        features: list[JsonValue] = [feature.value for feature in skill.features]
        required_fields: list[JsonValue] = [
            cast(JsonValue, field_name) for field_name in sorted(skill.required_task_fields)
        ]
        skill_payload: JsonObject = {
            "skillId": skill.skill_id,
            "capabilityId": skill.capability_id,
            "operation": skill.operation,
            "features": sorted(features, key=str),
            "requiredTaskFields": required_fields,
            "inputSchema": skill.input_schema,
            "outputSchema": skill.output_schema,
        }
        skills_payload.append(skill_payload)
    extension_payload: JsonObject = {
        "agentId": card.agent_id,
        "maxInputBytes": card.max_input_bytes,
        "skills": skills_payload,
    }
    extension = AgentExtension(
        uri=CAPABILITY_EXTENSION_URI,
        description="Misaka capability and schema metadata",
        required=False,
        params=_struct(extension_payload),
    )
    return AgentCard(
        name=card.name,
        description=card.description,
        supported_interfaces=[
            AgentInterface(
                url=interface_url,
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        version=card.version,
        capabilities=AgentCapabilities(
            streaming=CapabilityFeature.STREAMING in card.features,
            push_notifications=False,
            extended_agent_card=False,
            extensions=[extension],
        ),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(
                id=skill.skill_id,
                name=skill.name,
                description=skill.description,
                tags=[
                    f"capability:{skill.capability_id}",
                    f"operation:{skill.operation}",
                    *(
                        f"feature:{feature.value}"
                        for feature in sorted(skill.features, key=lambda value: value.value)
                    ),
                ],
                input_modes=["application/json", "text/plain"],
                output_modes=["application/json", "text/plain"],
            )
            for skill in card.skills
        ],
    )


def task_snapshot_to_proto(
    snapshot: TaskSnapshot,
    *,
    history_length: int | None = None,
    include_artifacts: bool = True,
) -> Task:
    result = snapshot.result
    metadata: JsonObject = {
        "sequence": len(snapshot.events),
        "taskStatus": snapshot.status.value,
    }
    if snapshot.invocation_id is not None:
        metadata["invocationId"] = snapshot.invocation_id
    if snapshot.delegation_id is not None:
        metadata["delegationId"] = snapshot.delegation_id
    if snapshot.activation_id is not None:
        metadata["activationId"] = snapshot.activation_id
    if result is not None and result.error_code is not None:
        metadata["errorCode"] = result.error_code
    if result is not None and result.error_message is not None:
        metadata["errorMessage"] = result.error_message
    if snapshot.status is TaskStatus.RECONCILIATION_REQUIRED:
        metadata["reconciliationRequired"] = True

    history = [_user_message(snapshot.request)]
    if result is not None and (result.output is not None or result.error_message):
        history.append(_result_message(snapshot))
    if history_length is not None:
        history = history[-history_length:] if history_length > 0 else []

    artifacts: list[Artifact] = []
    if include_artifacts and result is not None:
        artifacts = [
            Artifact(
                artifact_id=artifact.artifact_id,
                name=artifact.artifact_id,
                parts=[
                    Part(
                        url=artifact.location,
                        media_type=artifact.media_type,
                    )
                ],
                metadata=_struct(artifact.metadata),
            )
            for artifact in result.artifacts
        ]

    return Task(
        id=snapshot.request.task_id,
        context_id=snapshot.request.context_id,
        status=_proto_status(snapshot),
        artifacts=artifacts,
        history=history,
        metadata=_struct(metadata),
    )


def task_event_to_proto(event: TaskEvent, *, context_id: str) -> TaskStatusUpdateEvent:
    return TaskStatusUpdateEvent(
        task_id=event.task_id,
        context_id=context_id,
        status=ProtoTaskStatus(
            state=task_state_to_proto(event.status),
            timestamp=_timestamp(event.occurred_at),
        ),
        metadata=_struct(
            {
                "sequence": event.sequence,
                "taskStatus": event.status.value,
                "payload": event.payload,
            }
        ),
    )


def task_state_to_proto(status: TaskStatus) -> TaskState:
    return _PROTO_STATE_BY_STATUS[status]


def _proto_status(snapshot: TaskSnapshot) -> ProtoTaskStatus:
    message: Message | None = None
    if snapshot.result is not None and (
        snapshot.result.output is not None or snapshot.result.error_message
    ):
        message = _result_message(snapshot)
    timestamp = snapshot.events[-1].occurred_at if snapshot.events else datetime.now(UTC)
    return ProtoTaskStatus(
        state=task_state_to_proto(snapshot.status),
        message=message,
        timestamp=_timestamp(timestamp),
    )


def _user_message(request: TaskRequest) -> Message:
    return Message(
        message_id=request.message_id,
        context_id=request.context_id,
        task_id=request.task_id,
        role=Role.ROLE_USER,
        parts=[Part(data=_value(request.input), media_type="application/json")],
        metadata=_struct(request.metadata),
    )


def _result_message(snapshot: TaskSnapshot) -> Message:
    result = snapshot.result
    if result is None:
        raise ValueError("terminal result message requires a task result")
    parts: list[Part] = []
    if result.output is not None:
        parts.append(Part(data=_value(result.output), media_type="application/json"))
    if result.error_message is not None:
        parts.append(Part(text=result.error_message, media_type="text/plain"))
    return Message(
        message_id=f"result-{snapshot.request.task_id}",
        context_id=snapshot.request.context_id,
        task_id=snapshot.request.task_id,
        role=Role.ROLE_AGENT,
        parts=parts,
    )


def _input_from_parts(parts: Iterable[Part]) -> JsonObject:
    data_values: list[JsonValue] = []
    text_values: list[str] = []
    urls: list[str] = []
    for part in parts:
        kind = part.WhichOneof("content")
        if kind == "data":
            data_values.append(cast(JsonValue, MessageToDict(part.data)))
        elif kind == "text":
            text_values.append(part.text)
        elif kind == "url":
            urls.append(part.url)
        elif kind == "raw":
            raise ValueError("raw A2A input parts are not supported by this profile")
    if len(data_values) == 1 and isinstance(data_values[0], dict) and not text_values and not urls:
        return cast(JsonObject, data_values[0])
    result: JsonObject = {}
    if text_values:
        result["prompt"] = "\n".join(text_values)
    if data_values:
        result["data"] = data_values
    if urls:
        result["urls"] = cast(list[JsonValue], urls)
    if not result:
        raise ValueError("A2A message must contain at least one supported part")
    return result


def _features(value: JsonValue | None) -> frozenset[CapabilityFeature]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("requiredFeatures must be an array of strings")
    try:
        return frozenset(CapabilityFeature(item) for item in value)
    except ValueError as exc:
        raise ValueError(f"unsupported required feature: {exc}") from exc


def _session_ref(value: JsonValue | None) -> SessionRef | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("sessionRef must be an object")
    provider = value.get("provider")
    native_id = value.get("nativeId")
    if not isinstance(provider, str) or not isinstance(native_id, str):
        raise ValueError("sessionRef requires provider and nativeId strings")
    return SessionRef(provider=provider, native_id=native_id)


def _required_string(metadata: JsonObject, key: str) -> str:
    value = _optional_string(metadata, key)
    if value is None:
        raise ValueError(f"A2A message metadata.{key} is required")
    return value


def _optional_string(metadata: JsonObject, key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"A2A message metadata.{key} must be a non-empty string")
    return value


def _optional_object(value: JsonValue | None, name: str) -> JsonObject | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(JsonObject, value)


def _struct_to_object(value: Struct) -> JsonObject:
    return cast(JsonObject, MessageToDict(value))


def _struct(value: JsonObject) -> Struct:
    return ParseDict(value, Struct())


def _value(value: JsonValue) -> Value:
    return Parse(json.dumps(value, ensure_ascii=False), Value())


def _timestamp(value: datetime) -> Timestamp:
    timestamp = Timestamp()
    timestamp.FromDatetime(value)
    return timestamp


_PROTO_STATE_BY_STATUS: dict[TaskStatus, TaskState] = {
    TaskStatus.SUBMITTED: TaskState.TASK_STATE_SUBMITTED,
    TaskStatus.WORKING: TaskState.TASK_STATE_WORKING,
    TaskStatus.INPUT_REQUIRED: TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskStatus.CANCELLING: TaskState.TASK_STATE_WORKING,
    TaskStatus.REJECTED: TaskState.TASK_STATE_REJECTED,
    TaskStatus.COMPLETED: TaskState.TASK_STATE_COMPLETED,
    TaskStatus.FAILED: TaskState.TASK_STATE_FAILED,
    TaskStatus.CANCELLED: TaskState.TASK_STATE_CANCELED,
    TaskStatus.RECONCILIATION_REQUIRED: TaskState.TASK_STATE_FAILED,
}
