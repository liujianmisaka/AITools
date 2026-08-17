from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from multi_agent_v2.packages.domain.json_types import JsonObject


class EventDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_name: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)
    status: Literal["implemented", "contract_only"] = "implemented"
    producer: str = Field(min_length=1, max_length=256)
    consumers: tuple[str, ...] = Field(min_length=1)
    transport: str = Field(min_length=1, max_length=128)
    source_of_truth: str = Field(min_length=1, max_length=256)
    deduplication_key: str = Field(min_length=1, max_length=256)
    ordering: str = Field(min_length=1, max_length=512)
    persistent: bool
    temporal_history: bool
    payload_schema: JsonObject
    redaction: tuple[str, ...] = ()
