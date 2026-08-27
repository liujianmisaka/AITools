from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from agent_framework import FunctionTool
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from misaka_coordinator_service.domain._serialization import (
    datetime_to_text,
    ensure_optional_text,
    ensure_text,
    ensure_text_tuple,
    ensure_utc,
    read_datetime,
    read_optional_text,
    read_text,
    read_text_tuple,
)


class ToolRegistryError(RuntimeError):
    """Base error for tool discovery and invocation."""


class ToolRegistryConfigurationError(ToolRegistryError):
    """Raised when sources, schemas, or allow-lists conflict."""


class ToolNotAvailableError(ToolRegistryError):
    """Raised when a requested tool is not available in the current snapshot."""


class ToolArgumentsInvalidError(ToolRegistryError):
    """Raised when arguments do not satisfy a tool input schema."""


class ToolInvocationTimedOutError(ToolRegistryError):
    """Raised when a tool call exceeds the registry timeout."""


class ToolInvocationFailedError(ToolRegistryError):
    """Raised when a tool source fails to complete a call."""


class ToolSourceState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ToolInvocationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    source_id: str
    name: str
    description: str
    input_schema: Mapping[str, object]
    capability_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", ensure_text(self.source_id, "source_id"))
        object.__setattr__(self, "name", ensure_text(self.name, "name"))
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(
            self,
            "capability_ids",
            ensure_text_tuple(self.capability_ids, "capability_ids"),
        )
        try:
            Draft202012Validator.check_schema(self.input_schema)
        except SchemaError as error:
            raise ToolRegistryConfigurationError(
                f"tool {self.name} has an invalid input schema"
            ) from error


@dataclass(frozen=True, slots=True)
class ToolSourceStatus:
    source_id: str
    state: ToolSourceState
    tool_count: int
    error_message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", ensure_text(self.source_id, "source_id"))
        object.__setattr__(
            self,
            "error_message",
            ensure_optional_text(self.error_message, "error_message"),
        )
        if self.tool_count < 0:
            raise ToolRegistryConfigurationError("tool_count must not be negative")


@dataclass(frozen=True, slots=True)
class ToolAvailability:
    name: str
    available: bool
    source_id: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ToolInvocationAudit:
    invocation_id: str
    tool_name: str
    source_id: str | None
    outcome: ToolInvocationOutcome
    argument_names: tuple[str, ...]
    started_at: datetime
    finished_at: datetime
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "invocation_id",
            ensure_text(self.invocation_id, "invocation_id"),
        )
        object.__setattr__(self, "tool_name", ensure_text(self.tool_name, "tool_name"))
        object.__setattr__(
            self,
            "source_id",
            ensure_optional_text(self.source_id, "source_id"),
        )
        object.__setattr__(
            self,
            "argument_names",
            ensure_text_tuple(self.argument_names, "argument_names"),
        )
        object.__setattr__(self, "error_code", ensure_optional_text(self.error_code, "error_code"))
        started_at = ensure_utc(self.started_at, "started_at")
        object.__setattr__(self, "started_at", started_at)
        finished_at = ensure_utc(self.finished_at, "finished_at")
        if finished_at < started_at:
            raise ToolRegistryConfigurationError("finished_at must not be before started_at")
        object.__setattr__(self, "finished_at", finished_at)

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1000

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "invocation_id": self.invocation_id,
            "tool_name": self.tool_name,
            "source_id": self.source_id,
            "outcome": self.outcome.value,
            "argument_names": list(self.argument_names),
            "started_at": datetime_to_text(self.started_at),
            "finished_at": datetime_to_text(self.finished_at),
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ToolInvocationAudit:
        if data.get("schema_version") != 1:
            raise ToolRegistryError("unsupported tool audit schema version")
        return cls(
            invocation_id=read_text(data, "invocation_id"),
            tool_name=read_text(data, "tool_name"),
            source_id=read_optional_text(data, "source_id"),
            outcome=ToolInvocationOutcome(read_text(data, "outcome")),
            argument_names=read_text_tuple(data, "argument_names"),
            started_at=read_datetime(data, "started_at"),
            finished_at=read_datetime(data, "finished_at"),
            error_code=read_optional_text(data, "error_code"),
        )


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    invocation_id: str
    tool_name: str
    source_id: str
    value: object = field(repr=False)


class ToolSource(Protocol):
    @property
    def source_id(self) -> str: ...

    async def discover(self) -> Sequence[ToolDefinition]: ...

    async def invoke(self, tool_name: str, arguments: Mapping[str, object]) -> object: ...

    async def close(self) -> None: ...


class ToolAuditSink(Protocol):
    def record(self, audit: ToolInvocationAudit) -> None: ...


class InMemoryToolAuditSink:
    def __init__(self) -> None:
        self._records: list[ToolInvocationAudit] = []

    @property
    def records(self) -> tuple[ToolInvocationAudit, ...]:
        return tuple(self._records)

    def record(self, audit: ToolInvocationAudit) -> None:
        self._records.append(audit)


class JsonlToolAuditSink:
    """Append-only value-free audit persistence for Coordinator tool calls."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()

    @property
    def records(self) -> tuple[ToolInvocationAudit, ...]:
        with self._lock:
            if not self.path.exists():
                return ()
            records: list[ToolInvocationAudit] = []
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        value = cast(object, json.loads(line))
                        if not isinstance(value, dict):
                            raise ToolRegistryError(
                                f"tool audit line {line_number} must be an object"
                            )
                        raw = cast(dict[object, object], value)
                        if any(not isinstance(key, str) for key in raw):
                            raise ToolRegistryError(
                                f"tool audit line {line_number} keys must be strings"
                            )
                        records.append(ToolInvocationAudit.from_dict(cast(dict[str, object], raw)))
            except (OSError, json.JSONDecodeError) as error:
                raise ToolRegistryError("failed to read Coordinator tool audit log") from error
            return tuple(records)

    def record(self, audit: ToolInvocationAudit) -> None:
        line = json.dumps(audit.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as error:
                raise ToolRegistryError("failed to persist Coordinator tool audit") from error


@dataclass(frozen=True, slots=True)
class ToolRegistrySnapshot:
    revision: int
    tools: tuple[ToolDefinition, ...]
    sources: tuple[ToolSourceStatus, ...]
    refreshed_at: datetime


class MCPToolRegistry:
    def __init__(
        self,
        *,
        sources: Sequence[ToolSource],
        allowed_tool_names: Sequence[str],
        audit_sink: ToolAuditSink,
        invocation_timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        invocation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        source_ids = tuple(ensure_text(source.source_id, "source_id") for source in sources)
        if len(source_ids) != len(set(source_ids)):
            raise ToolRegistryConfigurationError("source_id values must be unique")
        allowed_names = tuple(
            ensure_text(name, "allowed_tool_names") for name in allowed_tool_names
        )
        if len(allowed_names) != len(set(allowed_names)):
            raise ToolRegistryConfigurationError("allowed_tool_names must not contain duplicates")
        if invocation_timeout_seconds <= 0:
            raise ToolRegistryConfigurationError(
                "invocation_timeout_seconds must be greater than zero"
            )
        self._sources = {source.source_id: source for source in sources}
        self._allowed_tool_names = frozenset(allowed_names)
        self._audit_sink = audit_sink
        self._invocation_timeout_seconds = invocation_timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._invocation_id_factory = invocation_id_factory or (lambda: str(uuid4()))
        self._tools: dict[str, ToolDefinition] = {}
        self._snapshot = ToolRegistrySnapshot(
            revision=0,
            tools=(),
            sources=tuple(
                ToolSourceStatus(
                    source_id=source_id,
                    state=ToolSourceState.UNAVAILABLE,
                    tool_count=0,
                    error_message="source has not been discovered",
                )
                for source_id in source_ids
            ),
            refreshed_at=ensure_utc(self._clock(), "clock"),
        )
        self._refresh_lock = asyncio.Lock()

    @property
    def snapshot(self) -> ToolRegistrySnapshot:
        return self._snapshot

    async def refresh(self) -> ToolRegistrySnapshot:
        async with self._refresh_lock:
            discovered_tools: dict[str, ToolDefinition] = {}
            statuses: list[ToolSourceStatus] = []
            for source_id, source in self._sources.items():
                try:
                    definitions = tuple(await source.discover())
                    source_tools = self._validate_source_definitions(source_id, definitions)
                except Exception as error:
                    statuses.append(
                        ToolSourceStatus(
                            source_id=source_id,
                            state=ToolSourceState.UNAVAILABLE,
                            tool_count=0,
                            error_message=f"{type(error).__name__}: {error}",
                        )
                    )
                    continue
                for definition in source_tools:
                    existing = discovered_tools.get(definition.name)
                    if existing is not None:
                        raise ToolRegistryConfigurationError(
                            f"tool {definition.name} is exposed by both "
                            f"{existing.source_id} and {definition.source_id}"
                        )
                    discovered_tools[definition.name] = definition
                statuses.append(
                    ToolSourceStatus(
                        source_id=source_id,
                        state=ToolSourceState.AVAILABLE,
                        tool_count=len(source_tools),
                    )
                )

            refreshed_at = ensure_utc(self._clock(), "clock")
            self._tools = discovered_tools
            self._snapshot = ToolRegistrySnapshot(
                revision=self._snapshot.revision + 1,
                tools=tuple(discovered_tools[name] for name in sorted(discovered_tools)),
                sources=tuple(statuses),
                refreshed_at=refreshed_at,
            )
            return self._snapshot

    def availability(self, tool_name: str) -> ToolAvailability:
        name = ensure_text(tool_name, "tool_name")
        definition = self._tools.get(name)
        if definition is not None:
            return ToolAvailability(
                name=name,
                available=True,
                source_id=definition.source_id,
                reason=None,
            )
        if name not in self._allowed_tool_names:
            reason = "tool is not in the registry allow-list"
        else:
            reason = "tool was not discovered from an available source"
        return ToolAvailability(name=name, available=False, source_id=None, reason=reason)

    def find_by_capability(self, capability_id: str) -> tuple[ToolDefinition, ...]:
        normalized = ensure_text(capability_id, "capability_id")
        return tuple(
            definition
            for definition in self._snapshot.tools
            if normalized in definition.capability_ids
        )

    def create_agent_tools(self) -> tuple[FunctionTool, ...]:
        tools: list[FunctionTool] = []
        for definition in self._snapshot.tools:

            async def invoke_proxy(
                _tool_name: str = definition.name,
                **arguments: object,
            ) -> object:
                result = await self.invoke(_tool_name, arguments)
                return result.value

            tools.append(
                FunctionTool(
                    name=definition.name,
                    description=definition.description,
                    approval_mode="never_require",
                    input_model=cast(Mapping[str, Any], definition.input_schema),
                    func=invoke_proxy,
                )
            )
        return tuple(tools)

    async def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> ToolCallResult:
        name = ensure_text(tool_name, "tool_name")
        invocation_id = ensure_text(self._invocation_id_factory(), "invocation_id")
        argument_names = tuple(sorted(ensure_text(key, "argument_name") for key in arguments))
        started_at = ensure_utc(self._clock(), "clock")
        definition = self._tools.get(name)
        if definition is None:
            error = ToolNotAvailableError(self.availability(name).reason or "unavailable")
            self._record_audit(
                invocation_id=invocation_id,
                tool_name=name,
                source_id=None,
                outcome=ToolInvocationOutcome.REJECTED,
                argument_names=argument_names,
                started_at=started_at,
                error_code="tool_not_available",
            )
            raise error

        try:
            validator = Draft202012Validator(definition.input_schema)
            validate = cast(Callable[[object], None], validator.validate)
            validate(dict(arguments))
        except ValidationError as error:
            self._record_audit(
                invocation_id=invocation_id,
                tool_name=name,
                source_id=definition.source_id,
                outcome=ToolInvocationOutcome.REJECTED,
                argument_names=argument_names,
                started_at=started_at,
                error_code="arguments_invalid",
            )
            raise ToolArgumentsInvalidError(
                f"arguments for tool {name} do not satisfy its input schema"
            ) from error

        source = self._sources[definition.source_id]
        try:
            async with asyncio.timeout(self._invocation_timeout_seconds):
                value = await source.invoke(name, dict(arguments))
        except TimeoutError as error:
            self._record_audit(
                invocation_id=invocation_id,
                tool_name=name,
                source_id=definition.source_id,
                outcome=ToolInvocationOutcome.TIMED_OUT,
                argument_names=argument_names,
                started_at=started_at,
                error_code="timeout",
            )
            raise ToolInvocationTimedOutError(
                f"tool {name} timed out after {self._invocation_timeout_seconds:g} seconds"
            ) from error
        except Exception as error:
            self._record_audit(
                invocation_id=invocation_id,
                tool_name=name,
                source_id=definition.source_id,
                outcome=ToolInvocationOutcome.FAILED,
                argument_names=argument_names,
                started_at=started_at,
                error_code="source_failure",
            )
            raise ToolInvocationFailedError(f"tool {name} failed") from error

        self._record_audit(
            invocation_id=invocation_id,
            tool_name=name,
            source_id=definition.source_id,
            outcome=ToolInvocationOutcome.SUCCEEDED,
            argument_names=argument_names,
            started_at=started_at,
        )
        return ToolCallResult(
            invocation_id=invocation_id,
            tool_name=name,
            source_id=definition.source_id,
            value=value,
        )

    async def close(self) -> None:
        errors: list[Exception] = []
        for source in self._sources.values():
            try:
                await source.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ToolRegistryError(f"failed to close {len(errors)} tool source(s)") from errors[0]

    def _validate_source_definitions(
        self,
        source_id: str,
        definitions: Sequence[ToolDefinition],
    ) -> tuple[ToolDefinition, ...]:
        tools: list[ToolDefinition] = []
        source_names: set[str] = set()
        for definition in definitions:
            if definition.source_id != source_id:
                raise ToolRegistryConfigurationError(
                    f"tool {definition.name} reports unexpected source_id {definition.source_id}"
                )
            if definition.name in source_names:
                raise ToolRegistryConfigurationError(
                    f"source {source_id} reports duplicate tool {definition.name}"
                )
            source_names.add(definition.name)
            if definition.name in self._allowed_tool_names:
                tools.append(definition)
        return tuple(tools)

    def _record_audit(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        source_id: str | None,
        outcome: ToolInvocationOutcome,
        argument_names: tuple[str, ...],
        started_at: datetime,
        error_code: str | None = None,
    ) -> None:
        self._audit_sink.record(
            ToolInvocationAudit(
                invocation_id=invocation_id,
                tool_name=tool_name,
                source_id=source_id,
                outcome=outcome,
                argument_names=argument_names,
                started_at=started_at,
                finished_at=ensure_utc(self._clock(), "clock"),
                error_code=error_code,
            )
        )
