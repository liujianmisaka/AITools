from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from misaka_capability_catalog import matches_json_schema
from misaka_interaction_contracts import PrincipalRef
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    JsonObject,
    JsonValue,
    ModuleId,
    ModuleManifest,
    ServiceProvision,
)
from misaka_resource_contracts import (
    CREDENTIAL_PROVIDER_SERVICE,
    RESOURCE_LEASE_PROVIDER_SERVICE,
    SANDBOX_PROVIDER_SERVICE,
    SETTINGS_PROVIDER_SERVICE,
    CredentialDescription,
    CredentialRef,
    FilesystemAccess,
    LeaseRequest,
    NetworkAccess,
    ResolvedCredential,
    ResourceLease,
    ResourceRef,
    SandboxCapabilities,
    SandboxGrant,
    SandboxRequirements,
    SecretValue,
    SettingsDefinition,
    SettingsSnapshot,
    SubprocessAccess,
)

from misaka_resource_capability.errors import (
    CredentialNotFound,
    LeaseExpired,
    ResourceBusy,
    ResourceFenced,
    SandboxUnavailable,
    SettingsConflict,
    SettingsNotFound,
)

Clock = Callable[[], datetime]


class MemoryResourceLeaseProvider:
    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._current: dict[tuple[str, str, str], ResourceLease] = {}
        self._last_epoch: dict[tuple[str, str, str], int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, request: LeaseRequest) -> ResourceLease:
        async with self._lock:
            now = self._clock()
            key = _resource_key(request.resource)
            current = self._current.get(key)
            if current is not None and current.active_at(now):
                if current.owner == request.owner and current.operation_id == request.operation_id:
                    return current
                raise ResourceBusy(
                    "lease.resource_busy",
                    f"resource {request.resource.resource_id} is leased by another owner",
                )
            epoch = self._last_epoch.get(key, 0) + 1
            lease = ResourceLease(
                resource=request.resource,
                owner=request.owner,
                operation_id=request.operation_id,
                epoch=epoch,
                token=uuid4().hex,
                acquired_at=now,
                expires_at=now + timedelta(seconds=request.ttl_seconds),
            )
            self._current[key] = lease
            self._last_epoch[key] = epoch
            return lease

    async def renew(self, lease: ResourceLease, *, ttl_seconds: float) -> ResourceLease:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        async with self._lock:
            now = self._clock()
            self._validate_unlocked(lease, now)
            renewed = ResourceLease(
                resource=lease.resource,
                owner=lease.owner,
                operation_id=lease.operation_id,
                epoch=lease.epoch,
                token=lease.token,
                acquired_at=lease.acquired_at,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._current[_resource_key(lease.resource)] = renewed
            return renewed

    async def transfer(
        self,
        lease: ResourceLease,
        owner: PrincipalRef,
        *,
        operation_id: str,
        ttl_seconds: float | None = None,
    ) -> ResourceLease:
        if not operation_id.strip():
            raise ValueError("resource lease operation id must not be empty")
        async with self._lock:
            now = self._clock()
            self._validate_unlocked(lease, now)
            if owner == lease.owner and operation_id == lease.operation_id:
                raise ValueError("resource lease transfer requires a different owner or operation")
            effective_ttl = ttl_seconds
            if effective_ttl is None:
                effective_ttl = (lease.expires_at - now).total_seconds()
            if effective_ttl <= 0:
                raise ValueError("resource lease ttl must be positive")
            key = _resource_key(lease.resource)
            epoch = self._last_epoch.get(key, lease.epoch) + 1
            transferred = ResourceLease(
                resource=lease.resource,
                owner=owner,
                operation_id=operation_id,
                epoch=epoch,
                token=uuid4().hex,
                acquired_at=now,
                expires_at=now + timedelta(seconds=effective_ttl),
            )
            self._current[key] = transferred
            self._last_epoch[key] = epoch
            return transferred

    async def validate(self, lease: ResourceLease) -> None:
        async with self._lock:
            self._validate_unlocked(lease, self._clock())

    async def release(self, lease: ResourceLease) -> None:
        async with self._lock:
            self._validate_unlocked(lease, self._clock())
            del self._current[_resource_key(lease.resource)]

    def _validate_unlocked(self, lease: ResourceLease, now: datetime) -> None:
        current = self._current.get(_resource_key(lease.resource))
        if current is None or (
            current.epoch != lease.epoch
            or current.token != lease.token
            or current.owner != lease.owner
            or current.operation_id != lease.operation_id
        ):
            raise ResourceFenced(
                "lease.fenced",
                f"lease epoch {lease.epoch} no longer owns the resource",
            )
        if not current.active_at(now):
            raise LeaseExpired("lease.expired", "resource lease has expired")


class StaticSandboxProvider:
    def __init__(
        self,
        capabilities: SandboxCapabilities,
        *,
        provider_id: str = "sandbox.static",
    ) -> None:
        if not provider_id.strip():
            raise ValueError("sandbox provider id must not be empty")
        self.capabilities = capabilities
        self.provider_id = provider_id
        self.resolutions = 0

    async def resolve(self, requirements: SandboxRequirements) -> SandboxGrant:
        self.resolutions += 1
        if _filesystem_rank(requirements.filesystem) > _filesystem_rank(
            self.capabilities.filesystem
        ):
            raise SandboxUnavailable(
                "sandbox.filesystem_unavailable",
                f"sandbox cannot enforce {requirements.filesystem.value} filesystem access",
            )
        if (
            requirements.network is NetworkAccess.ALLOW
            and self.capabilities.network is NetworkAccess.DENY
        ):
            raise SandboxUnavailable(
                "sandbox.network_unavailable",
                "sandbox cannot provide requested network access",
            )
        if (
            requirements.subprocess is SubprocessAccess.ALLOW
            and self.capabilities.subprocess is SubprocessAccess.DENY
        ):
            raise SandboxUnavailable(
                "sandbox.subprocess_unavailable",
                "sandbox cannot provide requested subprocess access",
            )
        allowed = set(self.capabilities.allowed_tools)
        if any(tool not in allowed for tool in requirements.allowed_tools):
            raise SandboxUnavailable(
                "sandbox.tool_unavailable",
                "sandbox cannot enforce the requested tool allowlist",
            )
        return SandboxGrant(
            grant_id=uuid4().hex,
            requirements=requirements,
            enforced_by=self.provider_id,
        )


@dataclass(slots=True)
class _CredentialState:
    ref: CredentialRef
    value: str | None
    revision: int
    metadata: JsonObject


class MemoryCredentialProvider:
    def __init__(self) -> None:
        self._credentials: dict[str, _CredentialState] = {}
        self._lock = asyncio.Lock()

    async def configure(
        self,
        ref: CredentialRef,
        value: str,
        *,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> CredentialDescription:
        public_metadata: JsonObject = dict(metadata or {})
        _reject_sensitive_metadata(public_metadata)
        async with self._lock:
            current = self._credentials.get(ref.credential_id)
            if current is not None and current.ref != ref:
                raise SettingsConflict(
                    "credential.purpose_conflict",
                    f"credential {ref.credential_id} is registered for another purpose",
                )
            revision = (current.revision if current is not None else 0) + 1
            state = _CredentialState(
                ref=ref,
                value=value or None,
                revision=revision,
                metadata=public_metadata,
            )
            self._credentials[ref.credential_id] = state
            return _credential_description(state)

    async def describe(self, ref: CredentialRef) -> CredentialDescription:
        async with self._lock:
            state = self._credentials.get(ref.credential_id)
            if state is None or state.ref != ref:
                return CredentialDescription(ref, configured=False, revision=0)
            return _credential_description(state)

    async def resolve(self, ref: CredentialRef) -> ResolvedCredential:
        async with self._lock:
            state = self._credentials.get(ref.credential_id)
            if state is None or state.ref != ref or state.value is None:
                raise CredentialNotFound(
                    "credential.not_configured",
                    f"credential {ref.credential_id} is not configured",
                )
            return ResolvedCredential(ref, SecretValue(state.value), state.revision)


@dataclass(slots=True)
class _SettingsState:
    definition: SettingsDefinition
    user_values: JsonObject
    revision: int


class MemorySettingsProvider:
    def __init__(self) -> None:
        self._settings: dict[str, _SettingsState] = {}
        self._lock = asyncio.Lock()

    async def define(self, definition: SettingsDefinition) -> SettingsSnapshot:
        async with self._lock:
            if definition.settings_id in self._settings:
                raise SettingsConflict(
                    "settings.already_defined",
                    f"settings {definition.settings_id} are already defined",
                )
            state = _SettingsState(definition, {}, 1)
            _validate_settings(state)
            self._settings[definition.settings_id] = state
            return _settings_snapshot(state)

    async def get(self, settings_id: str) -> SettingsSnapshot:
        async with self._lock:
            return _settings_snapshot(self._require(settings_id))

    async def update(
        self,
        settings_id: str,
        patch: JsonObject,
        *,
        expected_revision: int,
    ) -> SettingsSnapshot:
        async with self._lock:
            current = self._require(settings_id)
            _require_revision(current, expected_revision)
            updated = _SettingsState(
                current.definition,
                {**current.user_values, **patch},
                current.revision + 1,
            )
            _validate_settings(updated)
            self._settings[settings_id] = updated
            return _settings_snapshot(updated)

    async def replace(
        self,
        settings_id: str,
        values: JsonObject,
        *,
        expected_revision: int,
    ) -> SettingsSnapshot:
        async with self._lock:
            current = self._require(settings_id)
            _require_revision(current, expected_revision)
            updated = _SettingsState(
                current.definition,
                dict(values),
                current.revision + 1,
            )
            _validate_settings(updated)
            self._settings[settings_id] = updated
            return _settings_snapshot(updated)

    def _require(self, settings_id: str) -> _SettingsState:
        if not settings_id.strip():
            raise ValueError("settings id must not be empty")
        try:
            return self._settings[settings_id]
        except KeyError as exc:
            raise SettingsNotFound(
                "settings.not_found",
                f"settings {settings_id} were not found",
            ) from exc


MEMORY_RESOURCE_MODULE_ID = ModuleId("capability.resource.memory")


class MemoryResourceModule:
    """Registers the in-memory resource providers as one reversible module."""

    def __init__(
        self,
        *,
        leases: MemoryResourceLeaseProvider | None = None,
        sandbox: StaticSandboxProvider | None = None,
        credentials: MemoryCredentialProvider | None = None,
        settings: MemorySettingsProvider | None = None,
    ) -> None:
        self.leases = leases or MemoryResourceLeaseProvider()
        self.sandbox = sandbox or StaticSandboxProvider(SandboxCapabilities())
        self.credentials = credentials or MemoryCredentialProvider()
        self.settings = settings or MemorySettingsProvider()

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=MEMORY_RESOURCE_MODULE_ID,
            version="1.0.0",
            provides=(
                ServiceProvision(RESOURCE_LEASE_PROVIDER_SERVICE, "1.0.0"),
                ServiceProvision(SANDBOX_PROVIDER_SERVICE, "1.0.0"),
                ServiceProvision(CREDENTIAL_PROVIDER_SERVICE, "1.0.0"),
                ServiceProvision(SETTINGS_PROVIDER_SERVICE, "1.0.0"),
            ),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        context.provide(
            RESOURCE_LEASE_PROVIDER_SERVICE,
            self.leases,
            version="1.0.0",
        )
        context.provide(SANDBOX_PROVIDER_SERVICE, self.sandbox, version="1.0.0")
        context.provide(CREDENTIAL_PROVIDER_SERVICE, self.credentials, version="1.0.0")
        context.provide(SETTINGS_PROVIDER_SERVICE, self.settings, version="1.0.0")
        return None

    async def start(self, context: HostContext) -> None:
        del context


def _resource_key(resource: ResourceRef) -> tuple[str, str, str]:
    return resource.scope.scope_id, resource.resource_type, resource.resource_id


def _filesystem_rank(access: FilesystemAccess) -> int:
    return {
        FilesystemAccess.NONE: 0,
        FilesystemAccess.READ_ONLY: 1,
        FilesystemAccess.WRITE: 2,
    }[access]


def _credential_description(state: _CredentialState) -> CredentialDescription:
    return CredentialDescription(
        state.ref,
        configured=state.value is not None,
        revision=state.revision,
        metadata=dict(state.metadata),
    )


def _reject_sensitive_metadata(value: JsonValue, *, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if any(marker in normalized for marker in ("secret", "token", "password", "key")):
                raise ValueError(f"{path}.{key} may expose credential material")
            _reject_sensitive_metadata(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_metadata(item, path=f"{path}[{index}]")


def _merged_settings(state: _SettingsState) -> JsonObject:
    return {
        **state.definition.defaults,
        **state.definition.profile_base,
        **state.user_values,
    }


def _validate_settings(state: _SettingsState) -> None:
    if not matches_json_schema(_merged_settings(state), state.definition.schema):
        raise SettingsConflict(
            "settings.schema_violated",
            f"settings {state.definition.settings_id} do not satisfy their schema",
        )


def _require_revision(state: _SettingsState, expected_revision: int) -> None:
    if expected_revision != state.revision:
        raise SettingsConflict(
            "settings.revision_conflict",
            f"expected settings revision {expected_revision}, current revision is {state.revision}",
        )


def _settings_snapshot(state: _SettingsState) -> SettingsSnapshot:
    return SettingsSnapshot(
        settings_id=state.definition.settings_id,
        revision=state.revision,
        values=_merged_settings(state),
        schema=state.definition.schema,
        activation=state.definition.activation,
    )
