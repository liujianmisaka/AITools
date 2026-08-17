from __future__ import annotations

from collections.abc import Set
from datetime import timedelta

from multi_agent_v2.packages.delegation.models import (
    DelegationAdmission,
    DelegationDenied,
    DelegationRequest,
    DelegationUsage,
    ResourceBudget,
)


class DelegationPolicy:
    """Applies platform ceilings without granting any implicit parent permission."""

    def __init__(self, platform_budget: ResourceBudget) -> None:
        self._platform = platform_budget

    def admit(
        self,
        request: DelegationRequest,
        usage: DelegationUsage,
        *,
        provider_capabilities: Set[str],
    ) -> DelegationAdmission:
        effective = _minimum_budget(request.resource_budget, self._platform)
        missing = request.capability_requirements - provider_capabilities
        if missing:
            raise DelegationDenied(f"provider is missing required capabilities: {sorted(missing)}")
        if request.depth > effective.maximum_depth:
            raise DelegationDenied("delegation depth budget is exhausted")
        if usage.children_started >= effective.maximum_children:
            raise DelegationDenied("child count budget is exhausted")
        if usage.active_children >= effective.maximum_concurrency:
            raise DelegationDenied("child concurrency budget is exhausted")
        if usage.runtime_seconds >= effective.maximum_runtime_seconds:
            raise DelegationDenied("runtime budget is exhausted")
        if request.access_mode == "workspace_write" and (
            usage.workspace_write_children >= effective.maximum_workspace_write_children
        ):
            raise DelegationDenied("workspace-write child budget is exhausted")
        if effective.maximum_tokens is not None and usage.tokens >= effective.maximum_tokens:
            raise DelegationDenied("token budget is exhausted")
        if (
            effective.maximum_cost_microunits is not None
            and usage.cost_microunits >= effective.maximum_cost_microunits
        ):
            raise DelegationDenied("cost budget is exhausted")
        if usage.artifact_bytes >= effective.maximum_artifact_bytes:
            raise DelegationDenied("artifact budget is exhausted")
        return DelegationAdmission(
            request=request,
            remaining_runtime=timedelta(
                seconds=effective.maximum_runtime_seconds - usage.runtime_seconds
            ),
        )


def _minimum_budget(left: ResourceBudget, right: ResourceBudget) -> ResourceBudget:
    return ResourceBudget(
        maximum_children=min(left.maximum_children, right.maximum_children),
        maximum_depth=min(left.maximum_depth, right.maximum_depth),
        maximum_concurrency=min(left.maximum_concurrency, right.maximum_concurrency),
        maximum_runtime_seconds=min(
            left.maximum_runtime_seconds,
            right.maximum_runtime_seconds,
        ),
        maximum_tokens=_optional_min(left.maximum_tokens, right.maximum_tokens),
        maximum_cost_microunits=_optional_min(
            left.maximum_cost_microunits,
            right.maximum_cost_microunits,
        ),
        maximum_artifact_bytes=min(
            left.maximum_artifact_bytes,
            right.maximum_artifact_bytes,
        ),
        maximum_workspace_write_children=min(
            left.maximum_workspace_write_children,
            right.maximum_workspace_write_children,
        ),
    )


def _optional_min(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)
