from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from misaka_kernel_contracts.errors import ContractError

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class EventMode(StrEnum):
    EMIT = "emit"
    WATERFALL = "waterfall"
    SERIAL = "serial"
    PARALLEL = "parallel"
    BAIL = "bail"


class EventFailureIsolation(StrEnum):
    ISOLATE = "isolate"
    PROPAGATE = "propagate"


@dataclass(frozen=True, slots=True)
class EventDeclaration:
    """Versioned event contract used by the typed kernel dispatcher."""

    event_name: str
    version: int = 1
    mode: EventMode = EventMode.EMIT
    payload_schema: JsonObject = field(default_factory=dict)
    scope: str = "*"
    producer: str = "kernel"
    consumer: tuple[str, ...] = ("*",)
    failure_isolation: EventFailureIsolation = EventFailureIsolation.ISOLATE

    def __post_init__(self) -> None:
        if not self.event_name.strip():
            raise ContractError("event.declaration_name_empty", "event name must not be empty")
        if isinstance(self.version, bool) or self.version < 1:
            raise ContractError(
                "event.declaration_version_invalid",
                "event declaration version must be positive",
            )
        for field_name, value in {
            "scope": self.scope,
            "producer": self.producer,
        }.items():
            if not value.strip():
                raise ContractError(
                    f"event.declaration_{field_name}_empty",
                    f"event declaration {field_name} must not be empty",
                )
        if not self.consumer or any(not value.strip() for value in self.consumer):
            raise ContractError(
                "event.declaration_consumer_empty",
                "event declaration consumer must contain a non-empty value",
            )
        if len(self.consumer) != len(set(self.consumer)):
            raise ContractError(
                "event.declaration_consumer_duplicate",
                "event declaration consumers must be unique",
            )

    @property
    def name(self) -> str:
        return self.event_name


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    name: str
    payload: JsonObject = field(default_factory=dict)
    source: str = "kernel"
    correlation_id: str | None = None
    causation_id: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: int = 1
    scope_id: str = "global"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ContractError("event.name_empty", "event name must not be empty")
        if not self.source.strip():
            raise ContractError("event.source_empty", "event source must not be empty")
        if isinstance(self.version, bool) or self.version < 1:
            raise ContractError("event.version_invalid", "event version must be positive")
        if not self.scope_id.strip():
            raise ContractError("event.scope_empty", "event scope must not be empty")
        if self.occurred_at.tzinfo is None:
            raise ContractError("event.timestamp_naive", "event timestamp must be timezone-aware")


def matches_event_schema(value: JsonValue, schema: JsonObject) -> bool:
    """Validate the small JSON Schema subset used by event declarations."""

    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str) or not _matches_event_type(value, schema_type)
    ):
        return False
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or value not in enum):
        return False
    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            return False
        if any(item not in value for item in required):
            return False
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return False
        if schema.get("additionalProperties", True) is False and set(value) - set(properties):
            return False
        for key, property_value in value.items():
            property_schema = properties.get(key)
            if property_schema is not None and (
                not isinstance(property_schema, dict)
                or not matches_event_schema(property_value, property_schema)
            ):
                return False
    elif isinstance(value, list):
        items = schema.get("items")
        if items is not None and (
            not isinstance(items, dict)
            or not all(matches_event_schema(item, items) for item in value)
        ):
            return False
    return True


def _matches_event_type(value: JsonValue, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return False
