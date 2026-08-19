from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from misaka_invocation_contracts import (
    ArtifactRef,
    CapabilityFeature,
    CompletionBoundary,
    InvocationEvent,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ProviderExecutionRef,
    SessionRef,
    request_fingerprint,
)
from misaka_invocation_runtime import (
    IdempotencyConflict,
    InvocationError,
    InvocationSnapshot,
    InvocationStore,
)
from misaka_invocation_runtime.store import (
    ensure_invocation_transition,
    merge_provider_execution,
)
from misaka_kernel_contracts import JsonObject, JsonValue
from misaka_persistence_contracts import DurableConflict, DurableCorruption, DurableEvent
from misaka_persistence_jsonl import JsonlEventLog

_STREAM_PREFIX = "invocation:"


@dataclass(slots=True)
class _StoredInvocation:
    request: InvocationRequest
    fingerprint: str
    activation_id: str
    status: InvocationStatus
    events: list[InvocationEvent] = field(default_factory=list)
    result: InvocationResult | None = None
    provider_execution: ProviderExecutionRef | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


class JsonlInvocationStore(InvocationStore):
    """Rebuildable InvocationStore backed by a shared append-only JSONL log."""

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._records: dict[str, _StoredInvocation] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def open(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            await self._log.open()
            self._records.clear()
            self._idempotency.clear()
            grouped: dict[str, list[DurableEvent]] = {}
            for event in await self._log.all_events():
                if event.stream_id.startswith(_STREAM_PREFIX):
                    grouped.setdefault(event.stream_id, []).append(event)
            for stream_id, raw_events in grouped.items():
                events = tuple(sorted(raw_events, key=lambda item: item.sequence))
                self._load_stream(stream_id, events)
            self._loaded = True

    async def create(
        self,
        request: InvocationRequest,
    ) -> tuple[InvocationSnapshot, bool]:
        await self.open()
        fingerprint = request_fingerprint(request)
        async with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "invocation.idempotency_conflict",
                        f"idempotency key {request.idempotency_key} has a different request",
                    )
                return _snapshot(existing), False
            existing = self._records.get(request.invocation_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "invocation.id_conflict",
                        f"invocation id {request.invocation_id} has a different request",
                    )
                return _snapshot(existing), False

            now = datetime.now(UTC)
            initial_event = InvocationEvent(
                invocation_id=request.invocation_id,
                sequence=1,
                status=InvocationStatus.REGISTERED,
                occurred_at=now,
            )
            activation_id = f"{request.invocation_id}:activation:{request.attempt}"
            record = _StoredInvocation(
                request=request,
                fingerprint=fingerprint,
                activation_id=activation_id,
                status=InvocationStatus.REGISTERED,
                events=[initial_event],
            )
            payload: JsonObject = {
                "request": _encode_request(request),
                "fingerprint": fingerprint,
                "activation_id": activation_id,
                "event": _encode_event(initial_event),
            }
            try:
                await self._log.append(
                    _stream_id(request.invocation_id),
                    _created_event_id(request.invocation_id),
                    "invocation.created",
                    payload,
                )
            except DurableConflict as exc:
                raise InvocationError(
                    "invocation.durable_conflict",
                    str(exc),
                ) from exc
            self._records[request.invocation_id] = record
            self._idempotency[request.idempotency_key] = request.invocation_id
            return _snapshot(record), True

    async def list(self) -> tuple[InvocationSnapshot, ...]:
        await self.open()
        async with self._lock:
            return tuple(_snapshot(self._records[key]) for key in sorted(self._records))

    async def snapshot(self, invocation_id: str) -> InvocationSnapshot:
        await self.open()
        record = self._record(invocation_id)
        async with record.condition:
            return _snapshot(record)

    async def append_event(
        self,
        invocation_id: str,
        status: InvocationStatus,
        payload: JsonObject,
    ) -> InvocationEvent:
        await self.open()
        record = self._record(invocation_id)
        async with self._lock, record.condition:
            if record.result is not None:
                raise InvocationError(
                    "invocation.already_terminal",
                    f"invocation {invocation_id} is already terminal",
                )
            if status in _TERMINAL_STATUSES:
                raise InvocationError(
                    "invocation.terminal_requires_finalize",
                    "terminal invocation status must be written through finalize",
                )
            ensure_invocation_transition(record.status, status)
            event = InvocationEvent(
                invocation_id=invocation_id,
                sequence=len(record.events) + 1,
                status=status,
                payload=payload,
            )
            durable_payload: JsonObject = {"event": _encode_event(event)}
            await self._log.append(
                _stream_id(invocation_id),
                _event_id(invocation_id, event.sequence),
                "invocation.event",
                durable_payload,
            )
            record.events.append(event)
            record.status = status
            record.provider_execution = merge_provider_execution(
                record.provider_execution,
                payload,
            )
            record.condition.notify_all()
            return event

    async def finalize(self, result: InvocationResult) -> InvocationSnapshot:
        await self.open()
        record = self._record(result.invocation_id)
        async with self._lock, record.condition:
            if record.result is not None:
                if record.result != result:
                    raise InvocationError(
                        "invocation.terminal_conflict",
                        f"invocation {result.invocation_id} has a different terminal result",
                    )
                return _snapshot(record)
            ensure_invocation_transition(record.status, result.status)
            event = InvocationEvent(
                invocation_id=result.invocation_id,
                sequence=len(record.events) + 1,
                status=result.status,
                payload=_result_payload(result),
            )
            await self._log.append(
                _stream_id(result.invocation_id),
                _final_event_id(result.invocation_id),
                "invocation.finalized",
                {"event": _encode_event(event), "result": _encode_result(result)},
            )
            record.events.append(event)
            record.status = result.status
            record.result = result
            record.condition.notify_all()
            return _snapshot(record)

    async def wait_terminal(self, invocation_id: str) -> InvocationResult:
        await self.open()
        record = self._record(invocation_id)
        async with record.condition:
            while record.result is None:
                await record.condition.wait()
            return record.result

    async def events(
        self,
        invocation_id: str,
        *,
        start_sequence: int = 1,
    ) -> AsyncIterator[InvocationEvent]:
        if start_sequence < 1:
            raise ValueError("start_sequence must be at least one")
        await self.open()
        record = self._record(invocation_id)
        index = start_sequence - 1
        while True:
            async with record.condition:
                while index >= len(record.events) and record.result is None:
                    await record.condition.wait()
                if index < len(record.events):
                    event = record.events[index]
                    index += 1
                else:
                    return
            yield event

    def _record(self, invocation_id: str) -> _StoredInvocation:
        try:
            return self._records[invocation_id]
        except KeyError as exc:
            raise InvocationError(
                "invocation.not_found",
                f"invocation {invocation_id} was not found",
            ) from exc

    def _load_stream(self, stream_id: str, events: tuple[DurableEvent, ...]) -> None:
        if not events or events[0].event_type != "invocation.created":
            raise DurableCorruption(
                "invocation.missing_created",
                f"invocation stream {stream_id} must begin with invocation.created",
            )
        first = events[0]
        if first.sequence != 1:
            raise DurableCorruption(
                "invocation.sequence_invalid",
                f"invocation stream {stream_id} must start at sequence one",
            )
        record = _decode_created(first.payload)
        if _stream_id(record.request.invocation_id) != stream_id:
            raise DurableCorruption(
                "invocation.stream_identity_invalid",
                f"invocation stream {stream_id} does not match its request identity",
            )
        for durable_event in events[1:]:
            if durable_event.event_type == "invocation.event":
                event = _decode_event_payload(durable_event.payload)
                if event.status in _TERMINAL_STATUSES:
                    raise DurableCorruption(
                        "invocation.terminal_event_invalid",
                        "terminal statuses must be stored in invocation.finalized",
                    )
                self._apply_event(record, event)
            elif durable_event.event_type == "invocation.finalized":
                event = _decode_event_payload(durable_event.payload)
                result = _decode_result(durable_event.payload.get("result"))
                if event.status is not result.status or event.invocation_id != result.invocation_id:
                    raise DurableCorruption(
                        "invocation.final_event_mismatch",
                        f"invocation stream {stream_id} final event does not match result",
                    )
                self._apply_event(record, event)
                record.result = result
            else:
                raise DurableCorruption(
                    "invocation.unknown_event",
                    f"unknown invocation event type {durable_event.event_type}",
                )
        if record.result is not None and record.status not in _TERMINAL_STATUSES:
            raise DurableCorruption(
                "invocation.terminal_status_invalid",
                f"invocation stream {stream_id} has a non-terminal result status",
            )
        if record.request.invocation_id in self._records:
            raise DurableCorruption(
                "invocation.duplicate_stream",
                f"duplicate invocation stream for {record.request.invocation_id}",
            )
        self._records[record.request.invocation_id] = record
        existing_id = self._idempotency.get(record.request.idempotency_key)
        if existing_id is not None and existing_id != record.request.invocation_id:
            raise DurableCorruption(
                "invocation.duplicate_idempotency",
                f"idempotency key {record.request.idempotency_key} maps to two invocations",
            )
        self._idempotency[record.request.idempotency_key] = record.request.invocation_id

    @staticmethod
    def _apply_event(record: _StoredInvocation, event: InvocationEvent) -> None:
        expected_sequence = len(record.events) + 1
        if event.sequence != expected_sequence:
            raise DurableCorruption(
                "invocation.event_sequence_gap",
                f"expected invocation event sequence {expected_sequence}, got {event.sequence}",
            )
        try:
            ensure_invocation_transition(record.status, event.status)
        except InvocationError as exc:
            raise DurableCorruption(
                "invocation.transition_invalid",
                str(exc),
            ) from exc
        try:
            provider_execution = merge_provider_execution(
                record.provider_execution,
                event.payload,
            )
        except InvocationError as exc:
            raise DurableCorruption(
                "invocation.provider_fact_invalid",
                str(exc),
            ) from exc
        record.events.append(event)
        record.status = event.status
        record.provider_execution = provider_execution


def _stream_id(invocation_id: str) -> str:
    return f"{_STREAM_PREFIX}{invocation_id}"


def _created_event_id(invocation_id: str) -> str:
    return f"created:{invocation_id}"


def _event_id(invocation_id: str, sequence: int) -> str:
    return f"event:{invocation_id}:{sequence}"


def _final_event_id(invocation_id: str) -> str:
    return f"final:{invocation_id}"


def _snapshot(record: _StoredInvocation) -> InvocationSnapshot:
    return InvocationSnapshot(
        request=record.request,
        fingerprint=record.fingerprint,
        activation_id=record.activation_id,
        status=record.status,
        events=tuple(record.events),
        result=record.result,
        provider_execution=record.provider_execution,
    )


def _encode_request(request: InvocationRequest) -> JsonObject:
    return {
        "invocation_id": request.invocation_id,
        "capability_id": request.capability_id,
        "operation": request.operation,
        "input": request.input,
        "idempotency_key": request.idempotency_key,
        "completion_boundary": request.completion_boundary.value,
        "parent_invocation_id": request.parent_invocation_id,
        "session_ref": (
            {"provider": request.session_ref.provider, "native_id": request.session_ref.native_id}
            if request.session_ref is not None
            else None
        ),
        "required_features": cast(
            list[JsonValue],
            sorted(feature.value for feature in request.required_features),
        ),
        "output_schema": request.output_schema,
        "policy_context": request.policy_context,
        "attempt": request.attempt,
        "model": request.model,
        "effort": request.effort,
    }


def _decode_request(value: object) -> InvocationRequest:
    if not isinstance(value, dict):
        raise DurableCorruption(
            "invocation.request_invalid", "invocation request must be an object"
        )
    raw = cast(dict[object, object], value)
    session_value = raw.get("session_ref")
    session_ref: SessionRef | None = None
    try:
        if session_value is not None:
            if not isinstance(session_value, dict):
                raise TypeError("session_ref must be an object")
            session = cast(dict[object, object], session_value)
            session_ref = SessionRef(str(session["provider"]), str(session["native_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableCorruption(
            "invocation.session_ref_invalid", "session_ref has an invalid shape"
        ) from exc
    features_value = raw.get("required_features", [])
    if not isinstance(features_value, list):
        raise DurableCorruption(
            "invocation.features_invalid",
            "required_features must be a list",
        )
    try:
        feature_values = cast(list[object], features_value)
        features = frozenset(CapabilityFeature(str(item)) for item in feature_values)
        input_value = raw["input"]
        if not isinstance(input_value, dict):
            raise TypeError("input must be an object")
        output_schema = raw.get("output_schema")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise TypeError("output_schema must be an object")
        policy_context = raw.get("policy_context", {})
        if not isinstance(policy_context, dict):
            raise TypeError("policy_context must be an object")
        return InvocationRequest(
            invocation_id=str(raw["invocation_id"]),
            capability_id=str(raw["capability_id"]),
            operation=str(raw["operation"]),
            input=cast(JsonObject, input_value),
            idempotency_key=str(raw["idempotency_key"]),
            completion_boundary=CompletionBoundary(str(raw["completion_boundary"])),
            parent_invocation_id=_optional_string(raw.get("parent_invocation_id")),
            session_ref=session_ref,
            required_features=features,
            output_schema=cast(JsonObject | None, output_schema),
            policy_context=cast(JsonObject, policy_context),
            attempt=_int_value(raw.get("attempt", 1), "attempt"),
            model=_optional_string(raw.get("model")),
            effort=_optional_string(raw.get("effort")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableCorruption(
            "invocation.request_invalid",
            "invocation request has an invalid shape",
        ) from exc


def _encode_event(event: InvocationEvent) -> JsonObject:
    return {
        "invocation_id": event.invocation_id,
        "sequence": event.sequence,
        "status": event.status.value,
        "payload": event.payload,
        "occurred_at": event.occurred_at.isoformat(),
    }


def _decode_event_payload(value: object) -> InvocationEvent:
    if not isinstance(value, dict):
        raise DurableCorruption("invocation.event_invalid", "invocation event payload is invalid")
    outer = cast(dict[object, object], value)
    event_value = outer.get("event")
    if not isinstance(event_value, dict):
        raise DurableCorruption("invocation.event_invalid", "invocation event payload is invalid")
    raw = cast(dict[object, object], event_value)
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        raise DurableCorruption(
            "invocation.event_invalid", "invocation event payload must be an object"
        )
    try:
        return InvocationEvent(
            invocation_id=str(raw["invocation_id"]),
            sequence=_int_value(raw.get("sequence"), "sequence"),
            status=InvocationStatus(str(raw["status"])),
            payload=cast(JsonObject, payload),
            occurred_at=datetime.fromisoformat(str(raw["occurred_at"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableCorruption(
            "invocation.event_invalid", "invocation event has an invalid shape"
        ) from exc


def _decode_created(event_payload: JsonObject) -> _StoredInvocation:
    request = _decode_request(event_payload.get("request"))
    fingerprint = event_payload.get("fingerprint")
    activation_id = event_payload.get("activation_id")
    if (
        not isinstance(fingerprint, str)
        or not fingerprint.strip()
        or not isinstance(activation_id, str)
        or not activation_id.strip()
    ):
        raise DurableCorruption(
            "invocation.created_invalid", "created invocation metadata is invalid"
        )
    if fingerprint != request_fingerprint(request):
        raise DurableCorruption(
            "invocation.fingerprint_invalid", "invocation request fingerprint changed"
        )
    initial = _decode_event_payload({"event": event_payload.get("event")})
    if (
        initial.invocation_id != request.invocation_id
        or initial.sequence != 1
        or initial.status is not InvocationStatus.REGISTERED
    ):
        raise DurableCorruption(
            "invocation.created_event_invalid", "created invocation event is invalid"
        )
    return _StoredInvocation(
        request=request,
        fingerprint=fingerprint,
        activation_id=activation_id,
        status=InvocationStatus.REGISTERED,
        events=[initial],
    )


def _result_payload(result: InvocationResult) -> JsonObject:
    payload: JsonObject = {}
    if result.output is not None:
        payload["output"] = result.output
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    if result.error_message is not None:
        payload["error_message"] = result.error_message
    if result.artifacts:
        payload["artifacts"] = [_encode_artifact(artifact) for artifact in result.artifacts]
    return payload


def _encode_result(result: InvocationResult) -> JsonObject:
    return {
        "invocation_id": result.invocation_id,
        "status": result.status.value,
        "output": result.output,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "artifacts": [_encode_artifact(artifact) for artifact in result.artifacts],
    }


def _decode_result(value: object) -> InvocationResult:
    if not isinstance(value, dict):
        raise DurableCorruption("invocation.result_invalid", "invocation result must be an object")
    raw = cast(dict[object, object], value)
    artifacts_value = raw.get("artifacts", [])
    if not isinstance(artifacts_value, list):
        raise DurableCorruption("invocation.artifacts_invalid", "artifacts must be a list")
    try:
        return InvocationResult(
            invocation_id=str(raw["invocation_id"]),
            status=InvocationStatus(str(raw["status"])),
            output=cast(JsonValue | None, raw.get("output")),
            error_code=_optional_string(raw.get("error_code")),
            error_message=_optional_string(raw.get("error_message")),
            artifacts=tuple(_decode_artifact(item) for item in cast(list[object], artifacts_value)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableCorruption(
            "invocation.result_invalid", "invocation result has an invalid shape"
        ) from exc


def _encode_artifact(artifact: ArtifactRef) -> JsonObject:
    return {
        "artifact_id": artifact.artifact_id,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "location": artifact.location,
        "metadata": artifact.metadata,
    }


def _decode_artifact(value: object) -> ArtifactRef:
    if not isinstance(value, dict):
        raise DurableCorruption("invocation.artifact_invalid", "artifact must be an object")
    raw = cast(dict[object, object], value)
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise DurableCorruption(
            "invocation.artifact_invalid", "artifact metadata must be an object"
        )
    try:
        return ArtifactRef(
            artifact_id=str(raw["artifact_id"]),
            media_type=str(raw["media_type"]),
            size_bytes=_int_value(raw["size_bytes"], "size_bytes"),
            sha256=str(raw["sha256"]),
            location=str(raw["location"]),
            metadata=cast(JsonObject, metadata),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableCorruption(
            "invocation.artifact_invalid", "artifact has an invalid shape"
        ) from exc


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected a string or null")
    return value


def _int_value(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"{field_name} must be an integer")
    return int(value)


_TERMINAL_STATUSES = frozenset(
    {
        InvocationStatus.REJECTED,
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
        InvocationStatus.CANCELLED,
        InvocationStatus.RECONCILIATION_REQUIRED,
    }
)
