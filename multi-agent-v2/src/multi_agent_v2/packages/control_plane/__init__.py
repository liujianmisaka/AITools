"""Control-plane domain contracts and services."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from multi_agent_v2.packages.control_plane.activities import (
        RegisteredActivityRegistry as RegisteredActivityRegistry,
    )
    from multi_agent_v2.packages.control_plane.activities import (
        TemporalControlActivities as TemporalControlActivities,
    )
    from multi_agent_v2.packages.control_plane.models import (
        ApprovalDecision as ApprovalDecision,
    )
    from multi_agent_v2.packages.control_plane.models import (
        CommandAccepted as CommandAccepted,
    )
    from multi_agent_v2.packages.control_plane.models import (
        EventWaitRegistration as EventWaitRegistration,
    )
    from multi_agent_v2.packages.control_plane.models import GitRefTarget as GitRefTarget
    from multi_agent_v2.packages.control_plane.models import InstanceRecord as InstanceRecord
    from multi_agent_v2.packages.control_plane.models import InstanceStart as InstanceStart
    from multi_agent_v2.packages.control_plane.models import OutboxCommand as OutboxCommand
    from multi_agent_v2.packages.control_plane.models import (
        OutboxDispatchResult as OutboxDispatchResult,
    )
    from multi_agent_v2.packages.control_plane.models import ProjectionError as ProjectionError
    from multi_agent_v2.packages.control_plane.models import ProjectionEvent as ProjectionEvent
    from multi_agent_v2.packages.control_plane.models import (
        ProjectionNodeState as ProjectionNodeState,
    )
    from multi_agent_v2.packages.control_plane.models import ScheduleCreate as ScheduleCreate
    from multi_agent_v2.packages.control_plane.models import ScheduleRecord as ScheduleRecord
    from multi_agent_v2.packages.control_plane.models import ScheduleUpdate as ScheduleUpdate
    from multi_agent_v2.packages.control_plane.models import TemplateCreate as TemplateCreate
    from multi_agent_v2.packages.control_plane.models import TemplateRecord as TemplateRecord
    from multi_agent_v2.packages.control_plane.models import (
        TemplateVersionCreate as TemplateVersionCreate,
    )
    from multi_agent_v2.packages.control_plane.models import (
        TemplateVersionRecord as TemplateVersionRecord,
    )
    from multi_agent_v2.packages.control_plane.models import TriggerCreate as TriggerCreate
    from multi_agent_v2.packages.control_plane.models import TriggerRecord as TriggerRecord
    from multi_agent_v2.packages.control_plane.models import TriggerUpdate as TriggerUpdate
    from multi_agent_v2.packages.control_plane.models import (
        WorkflowSnapshotProjection as WorkflowSnapshotProjection,
    )
    from multi_agent_v2.packages.control_plane.service import (
        ControlPlaneService as ControlPlaneService,
    )
    from multi_agent_v2.packages.control_plane.service import (
        StaticWorkflowCatalog as StaticWorkflowCatalog,
    )
    from multi_agent_v2.packages.control_plane.service import WorkflowCatalog as WorkflowCatalog
    from multi_agent_v2.packages.control_plane.service import (
        WorkflowInputContractError as WorkflowInputContractError,
    )

_MODEL_EXPORTS = {
    "ApprovalDecision",
    "CommandAccepted",
    "EventWaitRegistration",
    "GitRefTarget",
    "InstanceRecord",
    "InstanceStart",
    "OutboxCommand",
    "OutboxDispatchResult",
    "ProjectionError",
    "ProjectionEvent",
    "ProjectionNodeState",
    "ScheduleCreate",
    "ScheduleRecord",
    "ScheduleUpdate",
    "TemplateCreate",
    "TemplateRecord",
    "TemplateVersionCreate",
    "TemplateVersionRecord",
    "TriggerCreate",
    "TriggerRecord",
    "TriggerUpdate",
    "WorkflowSnapshotProjection",
}
_SERVICE_EXPORTS = {
    "ControlPlaneService",
    "StaticWorkflowCatalog",
    "WorkflowCatalog",
    "WorkflowInputContractError",
}
_ACTIVITY_EXPORTS = {"RegisteredActivityRegistry", "TemporalControlActivities"}

__all__ = [
    "ApprovalDecision",
    "CommandAccepted",
    "ControlPlaneService",
    "EventWaitRegistration",
    "GitRefTarget",
    "InstanceRecord",
    "InstanceStart",
    "OutboxCommand",
    "OutboxDispatchResult",
    "ProjectionError",
    "ProjectionEvent",
    "ProjectionNodeState",
    "RegisteredActivityRegistry",
    "ScheduleCreate",
    "ScheduleRecord",
    "ScheduleUpdate",
    "StaticWorkflowCatalog",
    "TemplateCreate",
    "TemplateRecord",
    "TemplateVersionCreate",
    "TemplateVersionRecord",
    "TemporalControlActivities",
    "TriggerCreate",
    "TriggerRecord",
    "TriggerUpdate",
    "WorkflowCatalog",
    "WorkflowInputContractError",
    "WorkflowSnapshotProjection",
]


def __getattr__(name: str) -> object:
    if name in _MODEL_EXPORTS:
        module_name = "models"
    elif name in _SERVICE_EXPORTS:
        module_name = "service"
    elif name in _ACTIVITY_EXPORTS:
        module_name = "activities"
    else:
        raise AttributeError(name)
    module = import_module(f"{__name__}.{module_name}")
    value = cast(object, getattr(module, name))
    globals()[name] = value
    return value
