from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from misaka_invocation_contracts import CapabilityDescriptor, CapabilityFeature, CapabilityOperation
from misaka_kernel_contracts import JsonObject, JsonValue, ServiceKey

TOOL_CAPABILITY_ID = "tool.execution"
TOOL_OPERATION_EXECUTE = "execute"
TOOL_PROVIDER_SERVICE = ServiceKey("capability.tool.provider")


class ToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    tool_id: str
    display_name: str
    description: str
    input_schema: JsonObject
    output_schema: JsonObject = field(default_factory=dict)
    destructive: bool = False

    def __post_init__(self) -> None:
        if not self.tool_id.strip():
            raise ValueError("tool_id must not be empty")
        if not self.display_name.strip():
            raise ValueError("tool display_name must not be empty")
        if not self.description.strip():
            raise ValueError("tool description must not be empty")


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    invocation_id: str
    tool_id: str
    arguments: JsonObject
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name, value in {
            "invocation_id": self.invocation_id,
            "tool_id": self.tool_id,
            "idempotency_key": self.idempotency_key,
        }.items():
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class ToolResult:
    invocation_id: str
    tool_id: str
    status: ToolStatus
    output: JsonValue | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.invocation_id.strip() or not self.tool_id.strip():
            raise ValueError("tool result ids must not be empty")
        if self.status is ToolStatus.SUCCEEDED and self.error_code is not None:
            raise ValueError("successful tool result cannot contain an error code")
        if self.status is not ToolStatus.SUCCEEDED and self.error_code is None:
            raise ValueError("non-successful tool result must contain an error code")


ToolHandler = Callable[[JsonObject], JsonValue | Awaitable[JsonValue]]


class ToolProvider(Protocol):
    async def describe(self) -> CapabilityDescriptor: ...

    async def tools(self) -> tuple[ToolDescriptor, ...]: ...

    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...

    async def cancel(self, invocation_id: str, reason: str) -> None: ...

    async def close(self) -> None: ...


def tool_descriptor(
    *,
    features: frozenset[CapabilityFeature] = frozenset(),
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=TOOL_CAPABILITY_ID,
        version="1.0.0",
        operations=(CapabilityOperation(name=TOOL_OPERATION_EXECUTE),),
        features=features,
    )
