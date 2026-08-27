from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from misaka_coordinator_service.domain.errors import CoordinatorDomainError


def ensure_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoordinatorDomainError(f"{field_name} must be a non-empty string")
    return value.strip()


def ensure_optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return ensure_text(value, field_name)


def ensure_text_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(ensure_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise CoordinatorDomainError(f"{field_name} must not contain duplicates")
    return normalized


def ensure_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CoordinatorDomainError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def ensure_not_before(value: datetime, minimum: datetime, field_name: str) -> datetime:
    normalized = ensure_utc(value, field_name)
    if normalized < minimum:
        raise CoordinatorDomainError(f"{field_name} must not move backwards")
    return normalized


def datetime_to_text(value: datetime) -> str:
    return ensure_utc(value, "datetime").isoformat().replace("+00:00", "Z")


def read_text(data: Mapping[str, object], key: str) -> str:
    return ensure_text(data.get(key), key)


def read_optional_text(data: Mapping[str, object], key: str) -> str | None:
    return ensure_optional_text(data.get(key), key)


def read_int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinatorDomainError(f"{key} must be an integer")
    return value


def read_text_tuple(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        raise CoordinatorDomainError(f"{key} must be a list")
    items = cast(list[object], value)
    return ensure_text_tuple(tuple(ensure_text(item, key) for item in items), key)


def read_datetime(data: Mapping[str, object], key: str) -> datetime:
    raw = read_text(data, key)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise CoordinatorDomainError(f"{key} must be an ISO-8601 datetime") from error
    return ensure_utc(value, key)


def read_mapping(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise CoordinatorDomainError(f"{key} must be an object with string keys")
    raw = cast(dict[object, object], value)
    if any(not isinstance(item, str) for item in raw):
        raise CoordinatorDomainError(f"{key} must be an object with string keys")
    return cast(dict[str, object], raw)


def read_optional_mapping(data: Mapping[str, object], key: str) -> Mapping[str, object] | None:
    if data.get(key) is None:
        return None
    return read_mapping(data, key)


def read_mapping_list(data: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = data.get(key)
    if not isinstance(value, list):
        raise CoordinatorDomainError(f"{key} must be a list")
    items: list[Mapping[str, object]] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, dict):
            raise CoordinatorDomainError(f"{key}[{index}] must be an object with string keys")
        raw = cast(dict[object, object], item)
        if any(not isinstance(name, str) for name in raw):
            raise CoordinatorDomainError(f"{key}[{index}] must be an object with string keys")
        items.append(cast(dict[str, object], raw))
    return tuple(items)
