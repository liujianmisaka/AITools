from __future__ import annotations

from typing import Any, Literal

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


class ModelView(BaseModel):
    model_id: str
    display_name: str
    description: str
    supported_efforts: list[str]


class ModelCatalogView(BaseModel):
    provider_id: str
    models: list[ModelView]


class TemplateNodeSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    input: dict[str, Any]
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    provider_id: str | None = Field(default=None, min_length=1)
    output_schema: dict[str, Any] | None = None
    depends_on: list[str] = Field(default_factory=list)


class TemplateSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    name: str = Field(min_length=1)
    coordinator: Literal["direct", "dag"]
    nodes: list[TemplateNodeSubmission] = Field(min_length=1)


class TemplateView(TemplateSubmission):
    created_at: str


class InstanceSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)


class InstanceView(BaseModel):
    instance_id: str
    idempotency_key: str
    template_id: str
    template_version: int
    status: str
    version: int
    input: dict[str, Any]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str


class TriggerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    template_version: int = Field(ge=1)
    enabled: bool = True


class TriggerView(TriggerSubmission):
    created_at: str


class EventSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class EventDeliveryView(BaseModel):
    event_id: str
    event_type: str
    instance_ids: list[str]
