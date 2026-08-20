from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from misaka_interaction_contracts import PrincipalRef
from misaka_invocation_contracts import ArtifactRef
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import ModuleId, ModuleManifest, ServiceKey, ServiceProvision
from misaka_resource_contracts import FilesystemAccess, ResourceLease, SandboxGrant

WORKSPACE_SUPERVISOR_SERVICE = ServiceKey("capability.workspace.supervisor")
FAKE_WORKSPACE_MODULE_ID = ModuleId("capability.workspace.fake")


class WorkspaceAccess(StrEnum):
    READ_ONLY = "read_only"
    WRITE = "write"


class WorkspaceState(StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    IN_USE = "in_use"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"
    PRESERVED = "preserved"


@dataclass(frozen=True, slots=True)
class WorkspacePlan:
    workspace_id: str
    execution_id: str
    access: WorkspaceAccess
    owner: PrincipalRef
    lease: ResourceLease
    sandbox: SandboxGrant
    base_commit: str | None = None

    def __post_init__(self) -> None:
        if not self.workspace_id.strip() or not self.execution_id.strip():
            raise ValueError("workspace and execution ids must not be empty")
        if self.access is WorkspaceAccess.WRITE and not (self.base_commit or "").strip():
            raise ValueError("write workspace plans must pin a base commit")
        if self.lease.resource.resource_type != "workspace":
            raise ValueError("workspace plans require a workspace resource lease")
        if not self.lease.active_at():
            raise ValueError("workspace plans require an active resource lease")
        if self.lease.resource.resource_id != self.workspace_id:
            raise ValueError("workspace lease must target workspace_id")
        if self.lease.owner != self.owner:
            raise ValueError("workspace owner must match lease owner")
        required_access = (
            FilesystemAccess.WRITE
            if self.access is WorkspaceAccess.WRITE
            else FilesystemAccess.READ_ONLY
        )
        if _filesystem_rank(self.sandbox.requirements.filesystem) < _filesystem_rank(
            required_access
        ):
            raise ValueError("workspace plan exceeds the enforced sandbox filesystem grant")


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    plan: WorkspacePlan
    path: str
    state: WorkspaceState = WorkspaceState.READY


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupRequest:
    workspace: PreparedWorkspace
    lease: ResourceLease
    preserve: bool = False
    state_known: bool = True
    evidence: ArtifactRef | None = None

    def __post_init__(self) -> None:
        if self.lease.resource != self.workspace.plan.lease.resource:
            raise ValueError("workspace cleanup lease targets another resource")
        if not self.lease.active_at():
            raise ValueError("workspace cleanup requires an active resource lease")
        if self.lease.owner != self.workspace.plan.owner:
            raise ValueError("workspace cleanup owner does not match workspace owner")


class WorkspaceSupervisor(Protocol):
    async def prepare(self, plan: WorkspacePlan) -> PreparedWorkspace: ...

    async def cleanup(self, request: WorkspaceCleanupRequest) -> PreparedWorkspace: ...


class FakeWorkspaceSupervisor:
    def __init__(self) -> None:
        self._workspaces: dict[tuple[str, str], PreparedWorkspace] = {}

    async def prepare(self, plan: WorkspacePlan) -> PreparedWorkspace:
        key = (plan.workspace_id, plan.execution_id)
        existing = self._workspaces.get(key)
        if existing is not None:
            if existing.plan.lease.token == plan.lease.token:
                return existing
            if plan.lease.epoch <= existing.plan.lease.epoch:
                raise RuntimeError("workspace lease was fenced by a newer epoch")
            if existing.state not in {WorkspaceState.CLEANED, WorkspaceState.PRESERVED}:
                raise RuntimeError(
                    "workspace state is unresolved; preserve and reconcile before takeover"
                )
        prepared = PreparedWorkspace(
            plan,
            path=f"memory://workspace/{plan.workspace_id}/{plan.execution_id}",
        )
        self._workspaces[key] = prepared
        return prepared

    async def cleanup(self, request: WorkspaceCleanupRequest) -> PreparedWorkspace:
        workspace = request.workspace
        key = (workspace.plan.workspace_id, workspace.plan.execution_id)
        current = self._workspaces.get(key)
        if current is None:
            raise KeyError("workspace was not prepared")
        if (
            request.lease.epoch != current.plan.lease.epoch
            or request.lease.token != current.plan.lease.token
        ):
            raise RuntimeError("workspace cleanup lease was fenced by a newer epoch")
        if current.state in {WorkspaceState.CLEANED, WorkspaceState.PRESERVED}:
            return current
        must_preserve = (
            request.preserve
            or not request.state_known
            or (current.plan.access is WorkspaceAccess.WRITE and request.evidence is None)
        )
        state = WorkspaceState.PRESERVED if must_preserve else WorkspaceState.CLEANED
        finished = PreparedWorkspace(current.plan, current.path, state)
        self._workspaces[key] = finished
        return finished


class FakeWorkspaceModule:
    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=FAKE_WORKSPACE_MODULE_ID,
            version="1.0.0",
            provides=(ServiceProvision(WORKSPACE_SUPERVISOR_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        context.provide(
            WORKSPACE_SUPERVISOR_SERVICE,
            FakeWorkspaceSupervisor(),
            version="1.0.0",
        )
        return None

    async def start(self, context: HostContext) -> None:
        del context


def _filesystem_rank(access: FilesystemAccess) -> int:
    return {
        FilesystemAccess.NONE: 0,
        FilesystemAccess.READ_ONLY: 1,
        FilesystemAccess.WRITE: 2,
    }[access]
