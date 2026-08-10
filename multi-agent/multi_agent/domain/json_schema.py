from __future__ import annotations

from typing import Any


def validate_codex_output_schema(schema: dict[str, Any]) -> None:
    """Validate the strict JSON Schema subset required by Codex output_schema."""

    if schema.get("type") != "object":
        raise ValueError("root type must be 'object'")
    _validate_node(schema, path="$", require_object=True)


def _validate_node(
    node: Any,
    *,
    path: str,
    require_object: bool = False,
) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"{path} must be a JSON Schema object")

    node_type = node.get("type")
    object_type = (
        node_type == "object"
        or isinstance(node_type, list) and "object" in node_type
        or "properties" in node
    )
    if require_object and not object_type:
        raise ValueError(f"{path} must describe an object")

    if object_type:
        properties = node.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(f"{path}.properties must be an object")
        if node.get("additionalProperties") is not False:
            raise ValueError(f"{path}.additionalProperties must be false")

        required = node.get("required")
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            raise ValueError(f"{path}.required must list every property")
        missing = [name for name in properties if name not in required]
        if missing:
            raise ValueError(
                f"{path}.required is missing properties: {', '.join(missing)}"
            )

        for name, child in properties.items():
            _validate_node(child, path=f"{path}.properties.{name}")

    array_type = (
        node_type == "array"
        or isinstance(node_type, list) and "array" in node_type
    )
    if array_type:
        items = node.get("items")
        if not isinstance(items, dict):
            raise ValueError(f"{path}.items must be supplied for an array")
        _validate_node(items, path=f"{path}.items")

    definitions = node.get("$defs")
    if definitions is not None:
        if not isinstance(definitions, dict):
            raise ValueError(f"{path}.$defs must be an object")
        for name, child in definitions.items():
            _validate_node(child, path=f"{path}.$defs.{name}")

    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = node.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list):
            raise ValueError(f"{path}.{keyword} must be an array")
        for index, child in enumerate(branches):
            _validate_node(child, path=f"{path}.{keyword}[{index}]")
