from __future__ import annotations

import pytest
from misaka_process_capability import (
    FakeProcessSupervisor,
    ProcessSpec,
    ProcessState,
)
from misaka_workspace_capability import (
    FakeWorkspaceSupervisor,
    PreparedWorkspace,
    WorkspaceAccess,
    WorkspacePlan,
    WorkspaceState,
)


@pytest.mark.asyncio
async def test_fake_process_supervisor_tracks_identity_and_termination() -> None:
    supervisor = FakeProcessSupervisor()
    handle = await supervisor.start(ProcessSpec(("codex", "exec")))

    assert handle.identity.pid == 1
    await handle.terminate("test cancellation")
    result = await handle.wait()

    assert result.state is ProcessState.CANCELLED


@pytest.mark.asyncio
async def test_fake_workspace_is_idempotent_and_preserves_evidence() -> None:
    supervisor = FakeWorkspaceSupervisor()
    plan = WorkspacePlan(
        workspace_id="repo",
        execution_id="execution-1",
        access=WorkspaceAccess.WRITE,
        base_commit="abc123",
    )
    prepared = await supervisor.prepare(plan)
    repeated = await supervisor.prepare(plan)
    preserved = await supervisor.cleanup(prepared, preserve=True)
    repeated_cleanup = await supervisor.cleanup(prepared, preserve=False)

    assert prepared == repeated
    assert isinstance(preserved, PreparedWorkspace)
    assert preserved.state is WorkspaceState.PRESERVED
    assert repeated_cleanup.state is WorkspaceState.PRESERVED


def test_write_workspace_requires_pinned_base_commit() -> None:
    with pytest.raises(ValueError, match="base commit"):
        WorkspacePlan("repo", "execution-1", WorkspaceAccess.WRITE)
