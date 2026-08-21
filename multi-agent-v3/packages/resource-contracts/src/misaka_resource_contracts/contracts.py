from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from misaka_interaction_contracts import PrincipalRef, ScopeRef
from misaka_kernel_contracts import ContractError, JsonObject, ServiceKey

RESOURCE_LEASE_PROVIDER_SERVICE = ServiceKey("resource.lease.provider")
SANDBOX_PROVIDER_SERVICE = ServiceKey("resource.sandbox.provider")
CREDENTIAL_PROVIDER_SERVICE = ServiceKey("resource.credential.provider")
SETTINGS_PROVIDER_SERVICE = ServiceKey("resource.settings.provider")


@dataclass(frozen=True, slots=True)
class ResourceRef:
    resource_type: str
    resource_id: str
    scope: ScopeRef

    def __post_init__(self) -> None:
        if not self.resource_type.strip() or not self.resource_id.strip():
            raise ContractError(
                "resource.ref_empty",
                "resource type and id must not be empty",
            )


@dataclass(frozen=True, slots=True)
class LeaseRequest:
    resource: ResourceRef
    owner: PrincipalRef
    operation_id: str
    ttl_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise ContractError(
                "lease.operation_id_empty",
                "lease operation id must not be empty",
            )
        if self.ttl_seconds <= 0:
            raise ContractError("lease.ttl_invalid", "lease ttl must be positive")


@dataclass(frozen=True, slots=True)
class ResourceLease:
    resource: ResourceRef
    owner: PrincipalRef
    operation_id: str
    epoch: int
    token: str
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.operation_id.strip() or not self.token.strip():
            raise ContractError(
                "lease.identity_empty",
                "lease operation id and token must not be empty",
            )
        if self.epoch < 1:
            raise ContractError("lease.epoch_invalid", "lease epoch must be positive")
        _require_aware(self.acquired_at, "lease.acquired_at_naive")
        _require_aware(self.expires_at, "lease.expires_at_naive")
        if self.expires_at <= self.acquired_at:
            raise ContractError("lease.expiry_invalid", "lease expiry must follow acquisition")

    def active_at(self, now: datetime | None = None) -> bool:
        value = now or datetime.now(UTC)
        _require_aware(value, "lease.current_time_naive")
        return value < self.expires_at


class FilesystemAccess(StrEnum):
    NONE = "none"
    READ_ONLY = "read_only"
    WRITE = "write"


class NetworkAccess(StrEnum):
    DENY = "deny"
    ALLOW = "allow"


class SubprocessAccess(StrEnum):
    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True, slots=True)
class SandboxRequirements:
    filesystem: FilesystemAccess = FilesystemAccess.NONE
    network: NetworkAccess = NetworkAccess.DENY
    subprocess: SubprocessAccess = SubprocessAccess.DENY
    allowed_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not tool.strip() for tool in self.allowed_tools):
            raise ContractError(
                "sandbox.tool_empty",
                "sandbox allowed tools must not contain empty values",
            )
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ContractError(
                "sandbox.tool_duplicate",
                "sandbox allowed tools must be unique",
            )


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    filesystem: FilesystemAccess = FilesystemAccess.NONE
    network: NetworkAccess = NetworkAccess.DENY
    subprocess: SubprocessAccess = SubprocessAccess.DENY
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SandboxGrant:
    grant_id: str
    requirements: SandboxRequirements
    enforced_by: str
    issued_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.grant_id.strip() or not self.enforced_by.strip():
            raise ContractError(
                "sandbox.grant_identity_empty",
                "sandbox grant id and enforcer must not be empty",
            )
        _require_aware(self.issued_at, "sandbox.issued_at_naive")


@dataclass(frozen=True, slots=True)
class CredentialRef:
    credential_id: str
    purpose: str

    def __post_init__(self) -> None:
        if not self.credential_id.strip() or not self.purpose.strip():
            raise ContractError(
                "credential.ref_empty",
                "credential id and purpose must not be empty",
            )


@dataclass(frozen=True, slots=True)
class CredentialDescription:
    ref: CredentialRef
    configured: bool
    revision: int
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ContractError(
                "credential.revision_invalid",
                "credential revision must not be negative",
            )


class SecretValue:
    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ContractError(
                "credential.secret_empty",
                "credential secret must not be empty",
            )
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    ref: CredentialRef
    secret: SecretValue
    revision: int

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ContractError(
                "credential.revision_invalid",
                "resolved credential revision must be positive",
            )


class SettingsActivation(StrEnum):
    LIVE = "live"
    RESTART = "restart"


@dataclass(frozen=True, slots=True)
class SettingsDefinition:
    settings_id: str
    schema: JsonObject
    defaults: JsonObject = field(default_factory=dict)
    profile_base: JsonObject = field(default_factory=dict)
    activation: SettingsActivation = SettingsActivation.LIVE

    def __post_init__(self) -> None:
        if not self.settings_id.strip():
            raise ContractError(
                "settings.id_empty",
                "settings id must not be empty",
            )


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    settings_id: str
    revision: int
    values: JsonObject
    schema: JsonObject
    activation: SettingsActivation

    def __post_init__(self) -> None:
        if not self.settings_id.strip():
            raise ContractError("settings.id_empty", "settings id must not be empty")
        if self.revision < 1:
            raise ContractError(
                "settings.revision_invalid",
                "settings revision must be positive",
            )


class ResourceLeaseProvider(Protocol):
    async def acquire(self, request: LeaseRequest) -> ResourceLease: ...

    async def renew(self, lease: ResourceLease, *, ttl_seconds: float) -> ResourceLease: ...

    async def transfer(
        self,
        lease: ResourceLease,
        owner: PrincipalRef,
        *,
        operation_id: str,
        ttl_seconds: float | None = None,
    ) -> ResourceLease: ...

    async def validate(self, lease: ResourceLease) -> None: ...

    async def release(self, lease: ResourceLease) -> None: ...


class SandboxProvider(Protocol):
    async def resolve(self, requirements: SandboxRequirements) -> SandboxGrant: ...


class CredentialProvider(Protocol):
    async def describe(self, ref: CredentialRef) -> CredentialDescription: ...

    async def resolve(self, ref: CredentialRef) -> ResolvedCredential: ...


class SettingsProvider(Protocol):
    async def get(self, settings_id: str) -> SettingsSnapshot: ...

    async def update(
        self,
        settings_id: str,
        patch: JsonObject,
        *,
        expected_revision: int,
    ) -> SettingsSnapshot: ...

    async def replace(
        self,
        settings_id: str,
        values: JsonObject,
        *,
        expected_revision: int,
    ) -> SettingsSnapshot: ...


def _require_aware(value: datetime, code: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(code, "timestamp must be timezone-aware")
