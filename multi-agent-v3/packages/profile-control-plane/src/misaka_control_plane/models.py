from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    input: dict[str, Any]
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    provider_id: str | None = Field(default=None, min_length=1)
    output_schema: dict[str, Any] | None = None
    max_attempts: int = Field(default=1, ge=1)


class JobView(BaseModel):
    job_id: str
    idempotency_key: str
    status: str
    version: int
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


class HealthView(BaseModel):
    status: str
    profile: str


class CapabilityView(BaseModel):
    capability_id: str
    version: str
    operations: list[str]
    features: list[str]
