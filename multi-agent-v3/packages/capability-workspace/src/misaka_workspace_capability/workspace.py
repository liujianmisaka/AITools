from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import ModuleId, ModuleManifest, ServiceKey, ServiceProvision

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
    base_commit: str | None = None

    def __post_init__(self) -> None:
        if not self.workspace_id.strip() or not self.execution_id.strip():
            raise ValueError("workspace and execution ids must not be empty")
        if self.access is WorkspaceAccess.WRITE and not (self.base_commit or "").strip():
            raise ValueError("write workspace plans must pin a base commit")


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    plan: WorkspacePlan
    path: str
    state: WorkspaceState = WorkspaceState.READY


class WorkspaceSupervisor(Protocol):
    async def prepare(self, plan: WorkspacePlan) -> PreparedWorkspace: ...

    async def cleanup(
        self, workspace: PreparedWorkspace, *, preserve: bool
    ) -> PreparedWorkspace: ...


class FakeWorkspaceSupervisor:
    def __init__(self) -> None:
        self._workspaces: dict[tuple[str, str], PreparedWorkspace] = {}

    async def prepare(self, plan: WorkspacePlan) -> PreparedWorkspace:
        key = (plan.workspace_id, plan.execution_id)
        existing = self._workspaces.get(key)
        if existing is not None:
            if existing.plan != plan:
                raise ValueError("workspace plan conflicts with an existing execution")
            if existing.state in {WorkspaceState.CLEANED, WorkspaceState.PRESERVED}:
                return existing
            return PreparedWorkspace(plan, existing.path, WorkspaceState.READY)
        prepared = PreparedWorkspace(
            plan,
            path=f"memory://workspace/{plan.workspace_id}/{plan.execution_id}",
        )
        self._workspaces[key] = prepared
        return prepared

    async def cleanup(
        self,
        workspace: PreparedWorkspace,
        *,
        preserve: bool,
    ) -> PreparedWorkspace:
        key = (workspace.plan.workspace_id, workspace.plan.execution_id)
        current = self._workspaces.get(key)
        if current is None:
            raise KeyError("workspace was not prepared")
        if current.state in {WorkspaceState.CLEANED, WorkspaceState.PRESERVED}:
            return current
        state = WorkspaceState.PRESERVED if preserve else WorkspaceState.CLEANED
        finished = PreparedWorkspace(workspace.plan, workspace.path, state)
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
