from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.domain.models import JsonModel

_STANDARD_ATTRIBUTES = frozenset(
    {
        "specversion",
        "id",
        "source",
        "type",
        "subject",
        "time",
        "datacontenttype",
        "dataschema",
    }
)


class CloudEventEnvelope(JsonModel):
    specversion: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1, max_length=512)
    source: str = Field(min_length=1, max_length=512)
    type: str = Field(min_length=1, max_length=256)
    subject: str | None = Field(default=None, max_length=512)
    time: datetime | None = None
    datacontenttype: str = Field(default="application/json", max_length=256)
    dataschema: str | None = Field(default=None, max_length=1024)
    data: JsonObject
    extensions: JsonObject = Field(default_factory=dict)

    @field_validator("id", "source", "type", "subject", "datacontenttype", "dataschema")
    @classmethod
    def text_attributes_must_not_contain_controls(cls, value: str | None) -> str | None:
        if value is not None and any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("CloudEvent text attributes must not contain control characters")
        return value

    @field_validator("extensions")
    @classmethod
    def extension_names_must_not_shadow_standard_fields(
        cls,
        value: JsonObject,
    ) -> JsonObject:
        if any(name.lower() in _STANDARD_ATTRIBUTES for name in value):
            raise ValueError("CloudEvent extensions must not shadow standard attributes")
        return value


class EventIngestResult(JsonModel):
    inbox_id: str
    duplicate: bool
    routed_instances: tuple[str, ...] = ()
    signalled_workflows: tuple[str, ...] = ()
