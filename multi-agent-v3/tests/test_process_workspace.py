from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from misaka_interaction_contracts import PrincipalKind, PrincipalRef, ScopeRef
from misaka_process_capability import (
    FakeProcessSupervisor,
    ProcessSpec,
    ProcessState,
)
from misaka_resource_capability import MemoryResourceLeaseProvider, StaticSandboxProvider
from misaka_resource_contracts import (
    FilesystemAccess,
    LeaseRequest,
    NetworkAccess,
    ResourceRef,
    SandboxCapabilities,
    SandboxGrant,
    SandboxRequirements,
    SubprocessAccess,
)
from misaka_workspace_capability import (
    FakeWorkspaceSupervisor,
    PreparedWorkspace,
    WorkspaceAccess,
    WorkspaceCleanupRequest,
    WorkspacePlan,
    WorkspaceState,
)

OWNER = PrincipalRef("test-owner", PrincipalKind.APPLICATION)
SCOPE = ScopeRef("test-scope")


async def _lease(resource_type: str, resource_id: str):
    provider = MemoryResourceLeaseProvider()
    lease = await provider.acquire(
        LeaseRequest(
            ResourceRef(resource_type, resource_id, SCOPE),
            OWNER,
            operation_id=f"op:{resource_id}",
        )
    )
    return provider, lease


def _sandbox(*, filesystem: FilesystemAccess, subprocess: SubprocessAccess):
    return StaticSandboxProvider(
        SandboxCapabilities(
            filesystem=filesystem,
            network=NetworkAccess.DENY,
            subprocess=subprocess,
        )
    )


@pytest.mark.asyncio
async def test_fake_process_supervisor_tracks_identity_and_termination() -> None:
    lease_provider, lease = await _lease("process", "process-1")
    grant = await _sandbox(
        filesystem=FilesystemAccess.NONE,
        subprocess=SubprocessAccess.ALLOW,
    ).resolve(SandboxRequirements(subprocess=SubprocessAccess.ALLOW))
    supervisor = FakeProcessSupervisor()
    handle = await supervisor.start(
        ProcessSpec(
            process_id="process-1",
            owner=OWNER,
            lease=lease,
            sandbox=grant,
            argv=("codex", "exec"),
        )
    )

    assert handle.identity.pid == 1
    assert handle.identity.lease_epoch == lease.epoch
    await handle.terminate("test cancellation")
    result = await handle.wait()

    assert result.state is ProcessState.CANCELLED
    await lease_provider.release(lease)


@pytest.mark.asyncio
async def test_fake_workspace_is_idempotent_and_preserves_evidence() -> None:
    lease_provider, lease = await _lease("workspace", "repo")
    grant = await _sandbox(
        filesystem=FilesystemAccess.WRITE,
        subprocess=SubprocessAccess.DENY,
    ).resolve(SandboxRequirements(filesystem=FilesystemAccess.WRITE))
    supervisor = FakeWorkspaceSupervisor()
    plan = WorkspacePlan(
        workspace_id="repo",
        execution_id="execution-1",
        access=WorkspaceAccess.WRITE,
        owner=OWNER,
        lease=lease,
        sandbox=grant,
        base_commit="abc123",
    )
    prepared = await supervisor.prepare(plan)
    repeated = await supervisor.prepare(plan)
    preserved = await supervisor.cleanup(WorkspaceCleanupRequest(prepared, lease, preserve=True))
    repeated_cleanup = await supervisor.cleanup(
        WorkspaceCleanupRequest(prepared, lease, preserve=False)
    )

    assert prepared == repeated
    assert isinstance(preserved, PreparedWorkspace)
    assert preserved.state is WorkspaceState.PRESERVED
    assert repeated_cleanup.state is WorkspaceState.PRESERVED
    await lease_provider.release(lease)


def test_write_workspace_requires_pinned_base_commit() -> None:
    now = datetime.now(UTC)
    from misaka_resource_contracts import ResourceLease

    lease = ResourceLease(
        ResourceRef("workspace", "repo", SCOPE),
        OWNER,
        "op:repo",
        1,
        "token",
        now,
        now + timedelta(seconds=30),
    )
    grant = SandboxGrant(
        grant_id="grant",
        requirements=SandboxRequirements(filesystem=FilesystemAccess.WRITE),
        enforced_by="test",
    )
    with pytest.raises(ValueError, match="base commit"):
        WorkspacePlan("repo", "execution-1", WorkspaceAccess.WRITE, OWNER, lease, grant)
