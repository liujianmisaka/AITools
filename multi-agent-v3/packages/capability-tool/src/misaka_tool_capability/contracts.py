from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from misaka_interaction_contracts import DecisionProposal, PrincipalRef, ScopeRef
from misaka_invocation_contracts import CapabilityDescriptor, CapabilityFeature, CapabilityOperation
from misaka_kernel_contracts import JsonObject, JsonValue, ServiceKey
from misaka_resource_contracts import (
    CredentialRef,
    LeaseRequest,
    ResolvedCredential,
    ResourceLease,
    SandboxGrant,
    SandboxRequirements,
    SettingsSnapshot,
)

TOOL_CAPABILITY_ID = "tool.execution"
TOOL_OPERATION_EXECUTE = "execute"
TOOL_PROVIDER_SERVICE = ServiceKey("capability.tool.provider")
TOOL_PIPELINE_SERVICE = ServiceKey("capability.tool.pipeline")


class ToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


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
class ToolExecutionRequest:
    invocation: ToolInvocation
    proposal: DecisionProposal
    sandbox: SandboxRequirements
    lease_requests: tuple[LeaseRequest, ...] = ()
    credential_refs: tuple[CredentialRef, ...] = ()
    settings_ids: tuple[str, ...] = ()
    policy_context: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected_effect = f"tool.execute:{self.invocation.tool_id}"
        if expected_effect not in self.proposal.requested_effects:
            raise ValueError(f"tool proposal must include effect {expected_effect}")
        if len(self.settings_ids) != len(set(self.settings_ids)) or any(
            not item.strip() for item in self.settings_ids
        ):
            raise ValueError("tool settings ids must be non-empty and unique")
        credential_ids = [ref.credential_id for ref in self.credential_refs]
        if len(credential_ids) != len(set(credential_ids)):
            raise ValueError("tool credential references must be unique")
        resource_keys: set[tuple[str, str, str]] = set()
        for request in self.lease_requests:
            if request.owner != self.proposal.created_by:
                raise ValueError("tool lease owner must match proposal principal")
            if request.resource.scope != self.proposal.scope:
                raise ValueError("tool lease scope must match proposal scope")
            if request.operation_id != self.invocation.invocation_id:
                raise ValueError("tool lease operation must match invocation id")
            key = (
                request.resource.resource_type,
                request.resource.resource_id,
                request.resource.scope.scope_id,
            )
            if key in resource_keys:
                raise ValueError("tool lease requests must target unique resources")
            resource_keys.add(key)


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    owner: PrincipalRef
    scope: ScopeRef
    sandbox: SandboxGrant
    leases: tuple[ResourceLease, ...] = ()
    credentials: tuple[ResolvedCredential, ...] = ()
    settings: tuple[SettingsSnapshot, ...] = ()

    def credential(self, credential_id: str) -> ResolvedCredential:
        for credential in self.credentials:
            if credential.ref.credential_id == credential_id:
                return credential
        raise KeyError(f"credential {credential_id} was not resolved")

    def setting(self, settings_id: str) -> SettingsSnapshot:
        for snapshot in self.settings:
            if snapshot.settings_id == settings_id:
                return snapshot
        raise KeyError(f"settings {settings_id} were not resolved")


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


ToolHandler = Callable[
    [JsonObject, ToolExecutionContext],
    JsonValue | Awaitable[JsonValue],
]


class ToolProvider(Protocol):
    async def describe(self) -> CapabilityDescriptor: ...

    async def tools(self) -> tuple[ToolDescriptor, ...]: ...

    async def execute(
        self,
        invocation: ToolInvocation,
        context: ToolExecutionContext,
    ) -> ToolResult: ...

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
