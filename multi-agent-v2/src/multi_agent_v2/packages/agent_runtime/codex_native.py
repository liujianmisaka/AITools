from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, cast

from pydantic import BaseModel

from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "cookie",
        "set_cookie",
        "private_key",
    }
)


def to_plain_data(value: Any, *, _depth: int = 0) -> JsonValue:  # pyright: ignore[reportExplicitAny]
    if _depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, BaseModel):
        return cast(JsonValue, value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return to_plain_data(asdict(value), _depth=_depth + 1)
    if isinstance(value, dict):
        mapping = cast(Mapping[object, object], value)
        return {str(key): to_plain_data(item, _depth=_depth + 1) for key, item in mapping.items()}
    if isinstance(value, (list, tuple, set)):
        items = cast(Iterable[object], value)
        return [to_plain_data(item, _depth=_depth + 1) for item in items]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_plain_data(to_dict(), _depth=_depth + 1)
        except TypeError:
            pass
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        mapping = cast(Mapping[object, object], attributes)
        return {
            str(key): to_plain_data(item, _depth=_depth + 1)
            for key, item in mapping.items()
            if not str(key).startswith("_")
        }
    return str(value)


def redact_payload(value: Any) -> JsonValue:  # pyright: ignore[reportExplicitAny]
    def redact(item: JsonValue) -> JsonValue:
        if isinstance(item, dict):
            result: JsonObject = {}
            for key, nested in item.items():
                normalized = key.lower().replace("-", "_")
                compact = normalized.replace("_", "")
                sensitive = (
                    normalized in _SENSITIVE_KEYS
                    or compact in {"apikey", "accesstoken", "refreshtoken", "idtoken"}
                    or normalized.endswith(
                        (
                            "_api_key",
                            "_password",
                            "_secret",
                            "_token",
                            "_private_key",
                        )
                    )
                )
                result[key] = "***" if sensitive else redact(nested)
            return result
        if isinstance(item, list):
            return [redact(nested) for nested in item]
        return item

    return redact(to_plain_data(value))


def native_event_type(value: Any) -> str:  # pyright: ignore[reportExplicitAny]
    event_type = getattr(value, "type", None) or getattr(value, "method", None)
    if isinstance(event_type, Enum):
        return str(event_type.value)
    return str(event_type) if event_type else type(value).__name__


def extract_text(value: JsonValue) -> str | None:
    preferred = (
        "final_response",
        "finalResponse",
        "result",
        "content",
        "text",
        "delta_content",
        "deltaContent",
        "delta",
        "message",
    )

    def find(item: JsonValue) -> str | None:
        if isinstance(item, str):
            return item or None
        if isinstance(item, dict):
            for key in preferred:
                if key in item and (result := find(item[key])):
                    return result
            for nested in item.values():
                if result := find(nested):
                    return result
        if isinstance(item, list):
            parts = [part for nested in item if (part := find(nested))]
            return "".join(parts) if parts else None
        return None

    return find(value)
