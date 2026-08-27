from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import cast

from misaka_coordinator_service.domain._serialization import (
    ensure_optional_text,
    ensure_text,
    ensure_text_tuple,
    ensure_utc,
)
from misaka_coordinator_service.domain.models import ExecutionReference

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def _empty_json_object() -> JsonObject:
    return {}


_PLAN_HASH = re.compile(r"^[0-9a-f]{64}$")


class V3ExecutionContractError(ValueError):
    """Raised when a V3 request or response violates the stable adapter contract."""


class DelegationMode(StrEnum):
    ONE_SHOT = "one_shot"
    CONTINUABLE = "continuable"


class DelegationStatus(StrEnum):
    PROPOSED = "proposed"
    ADMITTED = "admitted"
    PREPARING = "preparing"
    ACTIVE = "active"
    PAUSED = "paused"
    WAITING_INPUT = "waiting_input"
    REPORTING = "reporting"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RECONCILING = "reconciling"

    @property
    def terminal(self) -> bool:
        return self in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
            DelegationStatus.CANCELLED,
            DelegationStatus.RECONCILIATION_REQUIRED,
        }


class MessageDelivery(StrEnum):
    APPEND = "append"
    INTERRUPT_CONTINUE = "interrupt_continue"


class ReconciliationStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class V3ActorKind(StrEnum):
    HUMAN = "human"
    APPLICATION = "application"
    AGENT = "agent"
    SERVICE = "service"
    SYSTEM = "system"


class SessionStreamEventKind(StrEnum):
    SNAPSHOT = "delegation.session.snapshot"
    EVENT = "delegation.session.event"
    END = "delegation.session.end"


@dataclass(frozen=True, slots=True)
class ExecutionSelection:
    provider_id: str
    model: str
    effort: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", ensure_text(self.provider_id, "provider_id"))
        object.__setattr__(self, "model", ensure_text(self.model, "model"))
        object.__setattr__(self, "effort", ensure_text(self.effort, "effort"))


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    prompt: str
    cwd: str
    selection: ExecutionSelection
    mode: DelegationMode = DelegationMode.ONE_SHOT
    delegation_id: str | None = None
    idempotency_key: str | None = None
    session_id: str | None = None
    channel_id: str | None = None
    parent_delegation_id: str | None = None
    input: Mapping[str, JsonValue] = field(default_factory=_empty_json_object)
    output_schema: Mapping[str, JsonValue] | None = None
    plan_hash: str | None = None
    decision_ref: Mapping[str, JsonValue] | None = None
    required_features: tuple[str, ...] = ()
    observers: tuple[Mapping[str, JsonValue], ...] = ()
    policy: Mapping[str, JsonValue] = field(default_factory=_empty_json_object)

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompt", ensure_text(self.prompt, "prompt"))
        object.__setattr__(self, "cwd", ensure_text(self.cwd, "cwd"))
        for field_name in (
            "delegation_id",
            "idempotency_key",
            "session_id",
            "channel_id",
            "parent_delegation_id",
        ):
            object.__setattr__(
                self,
                field_name,
                ensure_optional_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "required_features",
            ensure_text_tuple(self.required_features, "required_features"),
        )
        if self.plan_hash is not None and _PLAN_HASH.fullmatch(self.plan_hash) is None:
            raise V3ExecutionContractError("plan_hash must contain 64 lowercase hex digits")
        reserved = {"cwd", "sandbox", "provider_id", "model", "effort"}.intersection(self.input)
        if reserved:
            raise V3ExecutionContractError(
                f"delegation input contains reserved fields: {', '.join(sorted(reserved))}"
            )
        _validate_json_object(self.input, "input")
        if self.output_schema is not None:
            _validate_json_object(self.output_schema, "output_schema")
        if self.decision_ref is not None:
            _validate_json_object(self.decision_ref, "decision_ref")
        for index, observer in enumerate(self.observers):
            _validate_json_object(observer, f"observers[{index}]")
        _validate_json_object(self.policy, "policy")


@dataclass(frozen=True, slots=True)
class DelegationMessageRequest:
    delegation_id: str
    session_id: str
    message: str
    delivery: MessageDelivery = MessageDelivery.APPEND
    expected_activation_id: str | None = None
    dispatch_id: str | None = None
    idempotency_key: str | None = None
    message_id: str | None = None
    model: str | None = None
    effort: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("delegation_id", "session_id", "message"):
            object.__setattr__(self, field_name, ensure_text(getattr(self, field_name), field_name))
        for field_name in (
            "expected_activation_id",
            "dispatch_id",
            "idempotency_key",
            "message_id",
            "model",
            "effort",
        ):
            object.__setattr__(
                self,
                field_name,
                ensure_optional_text(getattr(self, field_name), field_name),
            )
        if (self.model is None) != (self.effort is None):
            raise V3ExecutionContractError("model and effort must be provided together")


@dataclass(frozen=True, slots=True)
class DelegationCancelRequest:
    delegation_id: str
    reason: str
    request_id: str | None = None
    idempotency_key: str | None = None
    session_id: str | None = None
    expected_activation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "delegation_id",
            ensure_text(self.delegation_id, "delegation_id"),
        )
        object.__setattr__(self, "reason", ensure_text(self.reason, "reason"))
        for field_name in (
            "request_id",
            "idempotency_key",
            "session_id",
            "expected_activation_id",
        ):
            object.__setattr__(
                self,
                field_name,
                ensure_optional_text(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, slots=True)
class DelegationReconciliationRequest:
    delegation_id: str
    expected_revision: int
    status: ReconciliationStatus
    reason: str
    output: JsonValue = None
    request_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "delegation_id",
            ensure_text(self.delegation_id, "delegation_id"),
        )
        object.__setattr__(self, "reason", ensure_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "request_id",
            ensure_optional_text(self.request_id, "request_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            ensure_optional_text(self.idempotency_key, "idempotency_key"),
        )
        if self.expected_revision < 1:
            raise V3ExecutionContractError("expected_revision must be positive")
        _validate_json_value(self.output, "output")
        if self.status is not ReconciliationStatus.COMPLETED and self.output is not None:
            raise V3ExecutionContractError("output is only valid for completed reconciliation")


@dataclass(frozen=True, slots=True)
class DelegationReport:
    status: DelegationStatus
    output: JsonValue
    artifact_ids: tuple[str, ...]
    error_code: str | None
    error_message: str | None
    source_invocation_id: str | None
    source_activation_id: str | None
    created_at: datetime

    @classmethod
    def from_object(cls, value: object) -> DelegationReport:
        data = _object(value, "report")
        output = _validate_json_value(data.get("output"), "report.output")
        return cls(
            status=DelegationStatus(_text(data, "status")),
            output=output,
            artifact_ids=_text_tuple(data, "artifact_ids"),
            error_code=_optional_text(data, "error_code"),
            error_message=_optional_text(data, "error_message"),
            source_invocation_id=_optional_text(data, "source_invocation_id"),
            source_activation_id=_optional_text(data, "source_activation_id"),
            created_at=_datetime(data, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class DelegationSnapshot:
    delegation_id: str
    status: DelegationStatus
    revision: int
    session_id: str | None
    channel_id: str | None
    parent_delegation_id: str | None
    depth: int
    current_invocation_id: str | None
    current_activation_id: str | None
    activation_count: int
    child_delegation_ids: tuple[str, ...]
    report: DelegationReport | None
    timed_out: bool = False
    waited_ms: int = 0
    next_action: str | None = None

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise V3ExecutionContractError("delegation revision must be positive")
        if self.depth < 0 or self.activation_count < 0 or self.waited_ms < 0:
            raise V3ExecutionContractError("delegation counters must not be negative")

    @property
    def execution_reference(self) -> ExecutionReference:
        return ExecutionReference(
            delegation_id=self.delegation_id,
            activation_id=self.current_activation_id,
            invocation_id=self.current_invocation_id,
            worker_session_id=self.session_id,
        )

    @classmethod
    def from_object(cls, value: object) -> DelegationSnapshot:
        data = _object(value, "delegation")
        status = DelegationStatus(_text(data, "status"))
        wire_terminal = data.get("terminal")
        if wire_terminal is not None and _boolean(data, "terminal") != status.terminal:
            raise V3ExecutionContractError("delegation terminal flag contradicts status")
        report_value = data.get("report")
        report = None if report_value is None else DelegationReport.from_object(report_value)
        return cls(
            delegation_id=_text(data, "delegation_id"),
            status=status,
            revision=_integer(data, "revision"),
            session_id=_optional_text(data, "session_id"),
            channel_id=_optional_text(data, "channel_id"),
            parent_delegation_id=_optional_text(data, "parent_delegation_id"),
            depth=_integer(data, "depth"),
            current_invocation_id=_optional_text(data, "current_invocation_id"),
            current_activation_id=_optional_text(data, "current_activation_id"),
            activation_count=_integer(data, "activation_count"),
            child_delegation_ids=_text_tuple(data, "child_delegation_ids"),
            report=report,
            timed_out=_optional_boolean(data, "timed_out", default=False),
            waited_ms=_optional_integer(data, "waited_ms", default=0),
            next_action=_optional_text(data, "next_action"),
        )


@dataclass(frozen=True, slots=True)
class MessageDispatchSnapshot:
    dispatch_id: str
    delegation_id: str
    session_id: str
    status: str
    revision: int
    applied_strategy: str | None
    previous_activation_id: str | None
    current_activation_id: str | None
    error_code: str | None
    error_message: str | None

    @classmethod
    def from_object(cls, value: object) -> MessageDispatchSnapshot:
        data = _object(value, "message_dispatch")
        revision = _integer(data, "revision")
        if revision < 1:
            raise V3ExecutionContractError("message dispatch revision must be positive")
        return cls(
            dispatch_id=_text(data, "dispatch_id"),
            delegation_id=_text(data, "delegation_id"),
            session_id=_text(data, "session_id"),
            status=_text(data, "status"),
            revision=revision,
            applied_strategy=_optional_text(data, "applied_strategy"),
            previous_activation_id=_optional_text(data, "previous_activation_id"),
            current_activation_id=_optional_text(data, "current_activation_id"),
            error_code=_optional_text(data, "error_code"),
            error_message=_optional_text(data, "error_message"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionModel:
    model_id: str
    display_name: str
    description: str
    supported_efforts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionProviderCatalog:
    provider_id: str
    models: tuple[ExecutionModel, ...]

    @classmethod
    def from_object(cls, value: object) -> ExecutionProviderCatalog:
        data = _object(value, "provider")
        models: list[ExecutionModel] = []
        for item in _object_sequence(data, "models"):
            models.append(
                ExecutionModel(
                    model_id=_text(item, "model_id"),
                    display_name=_text(item, "display_name"),
                    description=_text(item, "description"),
                    supported_efforts=_text_tuple(item, "supported_efforts"),
                )
            )
        return cls(provider_id=_text(data, "provider_id"), models=tuple(models))


@dataclass(frozen=True, slots=True)
class DelegationSessionEvent:
    delegation_id: str
    sequence: int
    kind: str
    invocation_id: str | None
    activation_id: str | None
    activation_number: int | None
    status: str | None
    provider_session_id: str | None
    provider_operation_id: str | None
    payload: JsonObject
    occurred_at: datetime

    @classmethod
    def from_object(cls, value: object) -> DelegationSessionEvent:
        data = _object(value, "session_event")
        sequence = _integer(data, "sequence")
        if sequence < 1:
            raise V3ExecutionContractError("session event sequence must be positive")
        activation_number = _optional_integer_or_none(data, "activation_number")
        if activation_number is not None and activation_number < 1:
            raise V3ExecutionContractError("activation_number must be positive")
        return cls(
            delegation_id=_text(data, "delegation_id"),
            sequence=sequence,
            kind=_text(data, "kind"),
            invocation_id=_optional_text(data, "invocation_id"),
            activation_id=_optional_text(data, "activation_id"),
            activation_number=activation_number,
            status=_optional_text(data, "status"),
            provider_session_id=_optional_text(data, "provider_session_id"),
            provider_operation_id=_optional_text(data, "provider_operation_id"),
            payload=_json_object(data.get("payload"), "payload"),
            occurred_at=_datetime(data, "occurred_at"),
        )


@dataclass(frozen=True, slots=True)
class DelegationSessionSnapshot:
    delegation: DelegationSnapshot
    provider_id: str | None
    model: str | None
    effort: str | None
    provider_session_id: str | None
    provider_operation_id: str | None
    activation_number: int
    last_sequence: int
    stage: str | None
    closed: bool
    updated_at: datetime | None

    @classmethod
    def from_object(cls, value: object) -> DelegationSessionSnapshot:
        data = _object(value, "session_snapshot")
        activation_number = _integer(data, "activation_number")
        last_sequence = _integer(data, "last_sequence")
        if activation_number < 0 or last_sequence < 0:
            raise V3ExecutionContractError("session counters must not be negative")
        updated_at = None
        if data.get("updated_at") is not None:
            updated_at = _datetime(data, "updated_at")
        return cls(
            delegation=DelegationSnapshot.from_object(data.get("delegation")),
            provider_id=_optional_text(data, "provider_id"),
            model=_optional_text(data, "model"),
            effort=_optional_text(data, "effort"),
            provider_session_id=_optional_text(data, "provider_session_id"),
            provider_operation_id=_optional_text(data, "provider_operation_id"),
            activation_number=activation_number,
            last_sequence=last_sequence,
            stage=_optional_text(data, "stage"),
            closed=_boolean(data, "closed"),
            updated_at=updated_at,
        )


@dataclass(frozen=True, slots=True)
class SessionStreamEvent:
    kind: SessionStreamEventKind
    event_id: str | None
    session_event: DelegationSessionEvent | None = None
    snapshot: DelegationSessionSnapshot | None = None
    next_sequence: int | None = None

    def __post_init__(self) -> None:
        populated = sum(
            value is not None for value in (self.session_event, self.snapshot, self.next_sequence)
        )
        if populated != 1:
            raise V3ExecutionContractError("session stream event must contain exactly one payload")
        if self.kind is SessionStreamEventKind.EVENT and self.session_event is None:
            raise V3ExecutionContractError("session event envelope requires session_event")
        if self.kind is SessionStreamEventKind.SNAPSHOT and self.snapshot is None:
            raise V3ExecutionContractError("session snapshot envelope requires snapshot")
        if self.kind is SessionStreamEventKind.END and self.next_sequence is None:
            raise V3ExecutionContractError("session end envelope requires next_sequence")
        if self.kind is SessionStreamEventKind.END and (
            isinstance(self.next_sequence, bool)
            or not isinstance(self.next_sequence, int)
            or self.next_sequence < 1
        ):
            raise V3ExecutionContractError("session end next_sequence must be positive")


def _object(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise V3ExecutionContractError(f"{field_name} must be an object")
    raw: dict[object, object] = dict(cast(Mapping[object, object], value))
    if any(not isinstance(key, str) for key in raw):
        raise V3ExecutionContractError(f"{field_name} keys must be strings")
    return cast(dict[str, object], raw)


def _text(data: Mapping[str, object], key: str) -> str:
    try:
        return ensure_text(data.get(key), key)
    except ValueError as error:
        raise V3ExecutionContractError(str(error)) from error


def _optional_text(data: Mapping[str, object], key: str) -> str | None:
    try:
        return ensure_optional_text(data.get(key), key)
    except ValueError as error:
        raise V3ExecutionContractError(str(error)) from error


def _integer(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise V3ExecutionContractError(f"{key} must be an integer")
    return value


def _optional_integer(data: Mapping[str, object], key: str, *, default: int) -> int:
    return default if data.get(key) is None else _integer(data, key)


def _optional_integer_or_none(data: Mapping[str, object], key: str) -> int | None:
    return None if data.get(key) is None else _integer(data, key)


def _boolean(data: Mapping[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise V3ExecutionContractError(f"{key} must be a boolean")
    return value


def _optional_boolean(data: Mapping[str, object], key: str, *, default: bool) -> bool:
    return default if data.get(key) is None else _boolean(data, key)


def _text_tuple(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        raise V3ExecutionContractError(f"{key} must be a list")
    items = cast(list[object], value)
    return tuple(_text({key: item}, key) for item in items)


def _object_sequence(data: Mapping[str, object], key: str) -> tuple[dict[str, object], ...]:
    value = data.get(key)
    if not isinstance(value, list):
        raise V3ExecutionContractError(f"{key} must be a list")
    items = cast(list[object], value)
    return tuple(_object(item, f"{key}[{index}]") for index, item in enumerate(items))


def _datetime(data: Mapping[str, object], key: str) -> datetime:
    raw = _text(data, key)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise V3ExecutionContractError(f"{key} must be an ISO-8601 datetime") from error
    return ensure_utc(value, key)


def _validate_json_object(value: Mapping[str, JsonValue], field_name: str) -> None:
    _json_object(value, field_name)


def _json_object(value: object, field_name: str) -> JsonObject:
    data = _object(value, field_name)
    return {key: _validate_json_value(item, f"{field_name}.{key}") for key, item in data.items()}


def _validate_json_value(value: object, field_name: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise V3ExecutionContractError(f"{field_name} must be finite")
        return value
    if isinstance(value, list):
        return [
            _validate_json_value(item, f"{field_name}[{index}]")
            for index, item in enumerate(cast(list[object], value))
        ]
    if isinstance(value, dict):
        raw = cast(dict[object, object], value)
        if any(not isinstance(key, str) for key in raw):
            raise V3ExecutionContractError(f"{field_name} keys must be strings")
        return _json_object(cast(dict[str, object], raw), field_name)
    raise V3ExecutionContractError(f"{field_name} must contain JSON values only")
