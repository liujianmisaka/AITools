from __future__ import annotations

import pytest
from pydantic import ValidationError

from multi_agent_v2.packages.delegation import (
    DelegationDenied,
    DelegationPolicy,
    DelegationRequest,
    DelegationUsage,
    ResourceBudget,
)


def _budget(**updates: int | None) -> ResourceBudget:
    values: dict[str, int | None] = {
        "maximum_children": 4,
        "maximum_depth": 3,
        "maximum_concurrency": 2,
        "maximum_runtime_seconds": 600,
        "maximum_tokens": 10_000,
        "maximum_cost_microunits": 1_000_000,
        "maximum_artifact_bytes": 1_000_000,
        "maximum_workspace_write_children": 1,
    }
    values.update(updates)
    return ResourceBudget.model_validate(values)


def _request(**updates: object) -> DelegationRequest:
    values: dict[str, object] = {
        "child_execution_id": "workflow-1:child:1",
        "parent_execution_id": "workflow-1:parent:1",
        "root_workflow_instance_id": "workflow-1",
        "lineage": ("workflow-1:parent:1",),
        "depth": 1,
        "provider": "fake",
        "model": "fake/model",
        "effort": "high",
        "workspace_id": "repo",
        "access_mode": "read_only",
        "capability_requirements": frozenset({"structured_output"}),
        "resource_budget": _budget(),
        "output_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
    values.update(updates)
    return DelegationRequest.model_validate(values)


def test_delegation_admission_applies_platform_and_request_budgets() -> None:
    admission = DelegationPolicy(_budget(maximum_children=2)).admit(
        _request(),
        DelegationUsage(children_started=1, runtime_seconds=100),
        provider_capabilities={"structured_output", "streaming"},
    )

    assert admission.admitted is True
    assert admission.remaining_runtime.total_seconds() == 500


def test_delegation_denies_missing_capabilities_and_write_budget() -> None:
    policy = DelegationPolicy(_budget())

    with pytest.raises(DelegationDenied, match="capabilities"):
        policy.admit(_request(), DelegationUsage(), provider_capabilities=set())
    with pytest.raises(DelegationDenied, match="workspace-write"):
        policy.admit(
            _request(access_mode="workspace_write"),
            DelegationUsage(children_started=1, workspace_write_children=1),
            provider_capabilities={"structured_output"},
        )


def test_delegation_lineage_is_acyclic_and_matches_depth() -> None:
    with pytest.raises(ValidationError, match="depth"):
        _request(depth=2)
    with pytest.raises(ValidationError, match="cycles"):
        _request(
            parent_execution_id="parent",
            lineage=("parent", "parent"),
            depth=2,
        )


def test_delegation_usage_rejects_impossible_child_counts() -> None:
    with pytest.raises(ValidationError, match="active child"):
        DelegationUsage(children_started=0, active_children=1)
    with pytest.raises(ValidationError, match="workspace-write"):
        DelegationUsage(children_started=0, workspace_write_children=1)
