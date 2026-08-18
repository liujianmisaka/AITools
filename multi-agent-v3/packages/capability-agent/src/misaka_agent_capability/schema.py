from __future__ import annotations

from misaka_kernel_contracts import JsonObject, JsonValue


def matches_json_schema(value: JsonValue, schema: JsonObject) -> bool:
    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str) or not _matches_type(value, schema_type)
    ):
        return False
    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            return False
        if any(item not in value for item in required):
            return False
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return False
        if schema.get("additionalProperties", True) is False and set(value) - set(properties):
            return False
        for key, property_value in value.items():
            property_schema = properties.get(key)
            if property_schema is None:
                continue
            if not isinstance(property_schema, dict) or not matches_json_schema(
                property_value,
                property_schema,
            ):
                return False
    elif isinstance(value, list):
        items = schema.get("items")
        if items is not None:
            if not isinstance(items, dict) or not all(
                matches_json_schema(item, items) for item in value
            ):
                return False
    return True


def _matches_type(value: JsonValue, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return False
