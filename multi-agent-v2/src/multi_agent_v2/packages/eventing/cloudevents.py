from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from cloudevents.http import from_http  # pyright: ignore[reportUnknownVariableType]
from pydantic import ValidationError

from multi_agent_v2.packages.domain.events import CloudEventEnvelope
from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue


class CloudEventParseError(ValueError):
    """An HTTP CloudEvent does not satisfy the platform envelope."""


def parse_http_cloud_event(
    headers: Mapping[str, str],
    body: bytes,
) -> CloudEventEnvelope:
    try:
        event = from_http(
            headers,
            body,
            data_unmarshaller=_unmarshal_json,
        )
        attributes = event.get_attributes()
        raw_data = event.get_data()
        if not isinstance(raw_data, dict):
            raise CloudEventParseError("CloudEvent data must be a JSON object")
        standard_names = {
            "specversion",
            "id",
            "source",
            "type",
            "subject",
            "time",
            "datacontenttype",
            "dataschema",
        }
        extensions = {
            str(name): cast(JsonValue, value)
            for name, value in attributes.items()
            if str(name).lower() not in standard_names
        }
        return CloudEventEnvelope.model_validate(
            {
                "specversion": attributes.get("specversion"),
                "id": attributes.get("id"),
                "source": attributes.get("source"),
                "type": attributes.get("type"),
                "subject": attributes.get("subject"),
                "time": attributes.get("time"),
                "datacontenttype": attributes.get(
                    "datacontenttype",
                    "application/json",
                ),
                "dataschema": attributes.get("dataschema"),
                "data": cast(JsonObject, raw_data),
                "extensions": extensions,
            }
        )
    except CloudEventParseError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise CloudEventParseError("invalid CloudEvent 1.0 HTTP envelope") from exc


def cloud_event_http_document(event: CloudEventEnvelope) -> JsonObject:
    document = event.model_dump(
        mode="json",
        by_alias=True,
        exclude={"extensions"},
        exclude_none=True,
    )
    document.update(event.extensions)
    return cast(JsonObject, document)


def event_time_or_received(event: CloudEventEnvelope, received_at: datetime) -> datetime:
    return event.time or received_at


def _unmarshal_json(value: str | bytes) -> object:
    return json.loads(value)
