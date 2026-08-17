from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from multi_agent_v2.packages.domain.json_types import JsonObject


class DelegationError(ValueError):
    code = "delegation.invalid"


class DelegationDenied(DelegationError):
    code = "delegation.denied"


class ResourceBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    maximum_children: int = Field(ge=0, le=1000)
    maximum_depth: int = Field(ge=0, le=100)
    maximum_concurrency: int = Field(ge=1, le=1000)
    maximum_runtime_seconds: int = Field(ge=1, le=604_800)
    maximum_tokens: int | None = Field(default=None, ge=1)
    maximum_cost_microunits: int | None = Field(default=None, ge=1)
    maximum_artifact_bytes: int = Field(ge=0, le=1_099_511_627_776)
    maximum_workspace_write_children: int = Field(ge=0, le=1000)


class DelegationUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    children_started: int = Field(default=0, ge=0)
    active_children: int = Field(default=0, ge=0)
    runtime_seconds: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_microunits: int = Field(default=0, ge=0)
    artifact_bytes: int = Field(default=0, ge=0)
    workspace_write_children: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_relations(self) -> DelegationUsage:
        if self.active_children > self.children_started:
            raise ValueError("active child count cannot exceed started child count")
        if self.workspace_write_children > self.children_started:
            raise ValueError("workspace-write child count cannot exceed started child count")
        return self


class DelegationRequest(BaseModel):
    """An explicit child execution request; it is not a scheduler or activation record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    child_execution_id: str = Field(min_length=1, max_length=512)
    parent_execution_id: str = Field(min_length=1, max_length=512)
    root_workflow_instance_id: str = Field(min_length=1, max_length=128)
    lineage: tuple[str, ...] = Field(min_length=1, max_length=100)
    depth: int = Field(ge=1, le=100)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    effort: str = Field(min_length=1, max_length=64)
    workspace_id: str = Field(min_length=1, max_length=128)
    access_mode: Literal["read_only", "workspace_write"]
    capability_requirements: frozenset[str] = frozenset()
    resource_budget: ResourceBudget
    output_schema: JsonObject

    @model_validator(mode="after")
    def validate_lineage(self) -> DelegationRequest:
        if self.child_execution_id == self.parent_execution_id:
            raise ValueError("child and parent execution IDs must differ")
        if self.lineage[-1] != self.parent_execution_id:
            raise ValueError("lineage must end with the parent execution ID")
        if len(self.lineage) != self.depth:
            raise ValueError("depth must equal the lineage length")
        if len(self.lineage) != len(set(self.lineage)):
            raise ValueError("lineage cannot contain cycles")
        return self


class DelegationAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: DelegationRequest
    admitted: Literal[True] = True
    remaining_runtime: timedelta
