from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decided_by: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)


class InstanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)
