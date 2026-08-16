"""Temporal workflow runtime boundary."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from multi_agent_v2.packages.workflow_runtime.messages import (
        WorkflowRunInput as WorkflowRunInput,
    )
    from multi_agent_v2.packages.workflow_runtime.workflow import (
        WorkflowInstanceWorkflow as WorkflowInstanceWorkflow,
    )

__all__ = ["WorkflowInstanceWorkflow", "WorkflowRunInput"]


def __getattr__(name: str) -> object:
    if name == "WorkflowRunInput":
        module_name = "messages"
    elif name == "WorkflowInstanceWorkflow":
        module_name = "workflow"
    else:
        raise AttributeError(name)
    module = import_module(f"{__name__}.{module_name}")
    value = cast(object, getattr(module, name))
    globals()[name] = value
    return value
