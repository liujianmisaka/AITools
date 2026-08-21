from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from misaka_artifact_capability import (
    ArtifactClassification,
    ArtifactWrite,
    MemoryArtifactStore,
)
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
from misaka_kernel import ProfileDefinition, ProfileLoader
from misaka_resource_capability import (
    MEMORY_RESOURCE_MODULE_ID,
    CredentialNotFound,
    LeaseExpired,
    MemoryCredentialProvider,
    MemoryResourceLeaseProvider,
    MemoryResourceModule,
    MemorySettingsProvider,
    ResourceFenced,
    SandboxUnavailable,
    SettingsConflict,
    StaticSandboxProvider,
)
from misaka_resource_contracts import (
    CREDENTIAL_PROVIDER_SERVICE,
    RESOURCE_LEASE_PROVIDER_SERVICE,
    SANDBOX_PROVIDER_SERVICE,
    SETTINGS_PROVIDER_SERVICE,
    CredentialRef,
    FilesystemAccess,
    LeaseRequest,
    NetworkAccess,
    ResourceRef,
    SandboxCapabilities,
    SandboxRequirements,
    SettingsDefinition,
    SubprocessAccess,
)
from misaka_workspace_capability import (
    FakeWorkspaceSupervisor,
    WorkspaceAccess,
    WorkspaceCleanupRequest,
    WorkspacePlan,
)

OWNER = PrincipalRef("resource-owner", PrincipalKind.APPLICATION)
OTHER_OWNER = PrincipalRef("other-owner", PrincipalKind.APPLICATION)
SCOPE = ScopeRef("resource-scope")


class _MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _lease_request(
    resource_type: str, resource_id: str, owner: PrincipalRef = OWNER
) -> LeaseRequest:
    return LeaseRequest(
        resource=ResourceRef(resource_type, resource_id, SCOPE),
        owner=owner,
        operation_id=f"operation:{resource_type}:{resource_id}",
        ttl_seconds=10,
    )


@pytest.mark.asyncio
async def test_lease_expiry_creates_new_epoch_and_fences_old_owner() -> None:
    clock = _MutableClock()
    provider = MemoryResourceLeaseProvider(clock=clock)
    first = await provider.acquire(_lease_request("workspace", "repo"))
    assert await provider.acquire(_lease_request("workspace", "repo")) == first

    clock.value += timedelta(seconds=11)
    second = await provider.acquire(_lease_request("workspace", "repo", OTHER_OWNER))
    assert second.epoch == first.epoch + 1
    with pytest.raises(ResourceFenced):
        await provider.validate(first)
    with pytest.raises(ResourceFenced):
        await provider.release(first)
    await provider.validate(second)


@pytest.mark.asyncio
async def test_lease_validation_reports_expired_owner_before_takeover() -> None:
    clock = _MutableClock()
    provider = MemoryResourceLeaseProvider(clock=clock)
    lease = await provider.acquire(_lease_request("process", "process-1"))
    clock.value += timedelta(seconds=11)
    with pytest.raises(LeaseExpired):
        await provider.validate(lease)


@pytest.mark.asyncio
async def test_resource_lease_transfer_advances_epoch_and_fences_previous_owner() -> None:
    provider = MemoryResourceLeaseProvider()
    first = await provider.acquire(_lease_request("artifact", "handoff"))

    transferred = await provider.transfer(
        first,
        OTHER_OWNER,
        operation_id="operation:artifact:handoff:transferred",
        ttl_seconds=10,
    )

    assert transferred.owner == OTHER_OWNER
    assert transferred.operation_id == "operation:artifact:handoff:transferred"
    assert transferred.epoch == first.epoch + 1
    assert transferred.token != first.token
    with pytest.raises(ResourceFenced):
        await provider.validate(first)
    await provider.validate(transferred)


@pytest.mark.asyncio
async def test_sandbox_provider_fails_closed_for_unsupported_effects() -> None:
    provider = StaticSandboxProvider(
        SandboxCapabilities(
            filesystem=FilesystemAccess.READ_ONLY,
            network=NetworkAccess.DENY,
            subprocess=SubprocessAccess.DENY,
            allowed_tools=("read_file",),
        )
    )
    with pytest.raises(SandboxUnavailable, match="network"):
        await provider.resolve(SandboxRequirements(network=NetworkAccess.ALLOW))
    with pytest.raises(SandboxUnavailable, match="subprocess"):
        await provider.resolve(SandboxRequirements(subprocess=SubprocessAccess.ALLOW))
    with pytest.raises(SandboxUnavailable, match="tool allowlist"):
        await provider.resolve(SandboxRequirements(allowed_tools=("shell",)))


@pytest.mark.asyncio
async def test_credentials_are_described_without_secret_material() -> None:
    provider = MemoryCredentialProvider()
    ref = CredentialRef("github-token", "git fetch")
    description = await provider.configure(ref, "top-secret", metadata={"owner": "test"})
    assert description.configured is True
    assert "top-secret" not in repr(description)
    resolved = await provider.resolve(ref)
    assert str(resolved.secret) == "<redacted>"
    assert "top-secret" not in repr(resolved.secret)
    with pytest.raises(ValueError, match="credential material"):
        await provider.configure(ref, "new-value", metadata={"api_token": "bad"})
    with pytest.raises(CredentialNotFound):
        await provider.resolve(CredentialRef("missing", "test"))


@pytest.mark.asyncio
async def test_settings_use_schema_and_expected_revision() -> None:
    provider = MemorySettingsProvider()
    snapshot = await provider.define(
        SettingsDefinition(
            "agent",
            schema={
                "type": "object",
                "required": ["region"],
                "properties": {"region": {"type": "string"}},
                "additionalProperties": False,
            },
            defaults={"region": "local"},
        )
    )
    assert snapshot.revision == 1
    updated = await provider.update("agent", {"region": "lan"}, expected_revision=1)
    assert updated.values["region"] == "lan"
    with pytest.raises(SettingsConflict, match="revision"):
        await provider.update("agent", {"region": "stale"}, expected_revision=1)
    with pytest.raises(SettingsConflict, match="schema"):
        await provider.replace("agent", {"unexpected": True}, expected_revision=2)


@pytest.mark.asyncio
async def test_artifact_commit_precedes_write_workspace_cleanup() -> None:
    leases = MemoryResourceLeaseProvider()
    workspace_lease = await leases.acquire(_lease_request("workspace", "repo"))
    sandbox = StaticSandboxProvider(SandboxCapabilities(filesystem=FilesystemAccess.WRITE))
    grant = await sandbox.resolve(SandboxRequirements(filesystem=FilesystemAccess.WRITE))
    workspace = await FakeWorkspaceSupervisor().prepare(
        WorkspacePlan(
            workspace_id="repo",
            execution_id="execution-1",
            access=WorkspaceAccess.WRITE,
            owner=OWNER,
            lease=workspace_lease,
            sandbox=grant,
            base_commit="abc123",
        )
    )
    supervisor = FakeWorkspaceSupervisor()
    workspace = await supervisor.prepare(workspace.plan)
    preserved = await supervisor.cleanup(
        WorkspaceCleanupRequest(workspace, workspace_lease, preserve=False)
    )
    assert preserved.state.value == "preserved"

    artifact_lease = await leases.acquire(_lease_request("artifact", "patch-1"))
    artifact = await MemoryArtifactStore().put(
        ArtifactWrite(
            artifact_key="patch-1",
            content=b"patch",
            media_type="text/plain",
            owner=OWNER,
            lease=artifact_lease,
            classification=ArtifactClassification.INTERNAL,
        )
    )
    supervisor = FakeWorkspaceSupervisor()
    workspace = await supervisor.prepare(workspace.plan)
    cleaned = await supervisor.cleanup(
        WorkspaceCleanupRequest(
            workspace,
            workspace_lease,
            preserve=False,
            evidence=artifact.artifact,
        )
    )
    assert cleaned.state.value == "cleaned"


@pytest.mark.asyncio
async def test_secret_artifacts_are_rejected() -> None:
    leases = MemoryResourceLeaseProvider()
    lease = await leases.acquire(_lease_request("artifact", "secret-1"))
    with pytest.raises(ValueError, match="secret material"):
        await MemoryArtifactStore().put(
            ArtifactWrite(
                artifact_key="secret-1",
                content=b"secret",
                media_type="text/plain",
                owner=OWNER,
                lease=lease,
                classification=ArtifactClassification.SECRET,
            )
        )


@pytest.mark.asyncio
async def test_memory_resource_module_registers_reversible_services() -> None:
    module = MemoryResourceModule()
    host = ProfileLoader({MEMORY_RESOURCE_MODULE_ID: lambda: module}).create_host(
        ProfileDefinition(
            profile_id="resource-test",
            module_ids=(MEMORY_RESOURCE_MODULE_ID,),
        )
    )
    await host.start()
    assert host.services.require(RESOURCE_LEASE_PROVIDER_SERVICE) is module.leases
    assert host.services.require(SANDBOX_PROVIDER_SERVICE) is module.sandbox
    assert host.services.require(CREDENTIAL_PROVIDER_SERVICE) is module.credentials
    assert host.services.require(SETTINGS_PROVIDER_SERVICE) is module.settings
    await host.stop()
    assert host.services.snapshot() == ()
