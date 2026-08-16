"""Workflow definition parsing and compilation boundary."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from multi_agent_v2.packages.workflow_dsl.compiler import (
        CompilationContext as CompilationContext,
    )
    from multi_agent_v2.packages.workflow_dsl.compiler import (
        ProviderModel as ProviderModel,
    )
    from multi_agent_v2.packages.workflow_dsl.compiler import (
        RegisteredActivity as RegisteredActivity,
    )
    from multi_agent_v2.packages.workflow_dsl.compiler import (
        compile_workflow as compile_workflow,
    )
    from multi_agent_v2.packages.workflow_dsl.errors import (
        WorkflowCompilationError as WorkflowCompilationError,
    )
    from multi_agent_v2.packages.workflow_dsl.ir import ExecutablePlan as ExecutablePlan
    from multi_agent_v2.packages.workflow_dsl.models import WorkflowDefinition as WorkflowDefinition
    from multi_agent_v2.packages.workflow_dsl.parser import (
        parse_json_workflow as parse_json_workflow,
    )
    from multi_agent_v2.packages.workflow_dsl.parser import (
        parse_yaml_workflow as parse_yaml_workflow,
    )

_EXPORTS = {
    "CompilationContext": ("compiler", "CompilationContext"),
    "ExecutablePlan": ("ir", "ExecutablePlan"),
    "ProviderModel": ("compiler", "ProviderModel"),
    "RegisteredActivity": ("compiler", "RegisteredActivity"),
    "WorkflowCompilationError": ("errors", "WorkflowCompilationError"),
    "WorkflowDefinition": ("models", "WorkflowDefinition"),
    "compile_workflow": ("compiler", "compile_workflow"),
    "parse_json_workflow": ("parser", "parse_json_workflow"),
    "parse_yaml_workflow": ("parser", "parse_yaml_workflow"),
}

__all__ = [
    "CompilationContext",
    "ExecutablePlan",
    "ProviderModel",
    "RegisteredActivity",
    "WorkflowCompilationError",
    "WorkflowDefinition",
    "compile_workflow",
    "parse_json_workflow",
    "parse_yaml_workflow",
]


def __getattr__(name: str) -> object:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    module = import_module(f"{__name__}.{module_name}")
    value = cast(object, getattr(module, attribute_name))
    globals()[name] = value
    return value
