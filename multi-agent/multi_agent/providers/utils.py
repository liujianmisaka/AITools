from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel

_SENSITIVE_KEYS = {
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


def to_plain_data(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return to_plain_data(asdict(value), _depth=_depth + 1)
    if isinstance(value, dict):
        return {
            str(key): to_plain_data(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [to_plain_data(item, _depth=_depth + 1) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return to_plain_data(value.to_dict(), _depth=_depth + 1)
        except TypeError:
            pass
    if hasattr(value, "__dict__"):
        return {
            key: to_plain_data(item, _depth=_depth + 1)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return repr(value)


def redact_payload(value: Any) -> Any:
    plain = to_plain_data(value)

    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for key, nested in item.items():
                normalized = key.lower().replace("-", "_")
                compact = normalized.replace("_", "")
                if (
                    normalized in _SENSITIVE_KEYS
                    or compact in {"apikey", "accesstoken", "refreshtoken", "idtoken"}
                    or normalized.endswith(
                        (
                            "_api_key",
                            "_password",
                            "_secret",
                            "_access_token",
                            "_refresh_token",
                            "_token",
                            "_private_key",
                        )
                    )
                ):
                    result[key] = "***"
                else:
                    result[key] = redact(nested)
            return result
        if isinstance(item, list):
            return [redact(nested) for nested in item]
        return item

    return redact(plain)


def extract_text(value: Any) -> str | None:
    plain = to_plain_data(value)
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

    def find(item: Any) -> str | None:
        if isinstance(item, str):
            return item if item else None
        if isinstance(item, dict):
            for key in preferred:
                if key in item:
                    result = find(item[key])
                    if result:
                        return result
            for nested in item.values():
                result = find(nested)
                if result:
                    return result
        if isinstance(item, list):
            parts = [part for nested in item if (part := find(nested))]
            if parts:
                return "".join(parts)
        return None

    return find(plain)


def native_event_type(value: Any) -> str:
    event_type = getattr(value, "type", None) or getattr(value, "method", None)
    if isinstance(event_type, Enum):
        return str(event_type.value)
    if event_type:
        return str(event_type)
    return type(value).__name__
