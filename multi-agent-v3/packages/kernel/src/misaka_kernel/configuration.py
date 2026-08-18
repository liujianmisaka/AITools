from __future__ import annotations

from typing import cast

from misaka_kernel_contracts import JsonObject, JsonValue, ModuleId

from misaka_kernel.errors import ModuleGraphError


def validate_configuration(
    module_id: ModuleId,
    schema: JsonObject,
    configuration: JsonObject,
) -> None:
    if not schema:
        return
    schema_type = schema.get("type", "object")
    if schema_type != "object":
        raise ModuleGraphError(
            "module.configuration_schema_invalid",
            f"module {module_id} configuration schema root must be an object",
        )

    required = _string_list(schema.get("required", []), module_id, "required")
    missing = sorted(name for name in required if name not in configuration)
    if missing:
        raise ModuleGraphError(
            "module.configuration_required",
            f"module {module_id} is missing configuration: {', '.join(missing)}",
        )

    raw_properties = schema.get("properties", {})
    if not isinstance(raw_properties, dict):
        raise ModuleGraphError(
            "module.configuration_schema_invalid",
            f"module {module_id} configuration properties must be an object",
        )
    properties = cast(dict[str, JsonValue], raw_properties)
    additional_properties = schema.get("additionalProperties", True)
    if not isinstance(additional_properties, bool):
        raise ModuleGraphError(
            "module.configuration_schema_invalid",
            f"module {module_id} additionalProperties must be boolean",
        )
    if not additional_properties:
        unknown = sorted(set(configuration) - set(properties))
        if unknown:
            raise ModuleGraphError(
                "module.configuration_unknown",
                f"module {module_id} has unknown configuration: {', '.join(unknown)}",
            )

    for name, value in configuration.items():
        raw_property_schema = properties.get(name)
        if raw_property_schema is None:
            continue
        if not isinstance(raw_property_schema, dict):
            raise ModuleGraphError(
                "module.configuration_schema_invalid",
                f"module {module_id} property {name} schema must be an object",
            )
        property_schema = cast(dict[str, JsonValue], raw_property_schema)
        expected_type = property_schema.get("type")
        if expected_type is None:
            continue
        if not isinstance(expected_type, str) or not _matches_type(value, expected_type):
            raise ModuleGraphError(
                "module.configuration_type",
                f"module {module_id} property {name} must be {expected_type}",
            )


def _string_list(value: JsonValue, module_id: ModuleId, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ModuleGraphError(
            "module.configuration_schema_invalid",
            f"module {module_id} schema field {field_name} must be a string array",
        )
    return tuple(cast(str, item) for item in value)


def _matches_type(value: JsonValue, expected_type: str) -> bool:
    if expected_type == "null":
        return value is None
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return False
