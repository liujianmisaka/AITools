from __future__ import annotations

from collections.abc import Iterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue
from multi_agent_v2.packages.workflow_dsl.errors import CompilationIssue, issue


def validate_strict_schema(
    schema: JsonObject,
    *,
    path: str,
    complete_required: bool,
) -> tuple[CompilationIssue, ...]:
    problems: list[CompilationIssue] = []
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        problems.append(issue("schema.invalid", path, exc.message))
        return tuple(problems)

    if schema.get("type") != "object":
        problems.append(issue("schema.root_not_object", path, "schema root type must be object"))

    for pointer, node in _walk_schema(schema, path):
        reference = node.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#/$defs/"):
            problems.append(
                issue(
                    "schema.remote_ref_forbidden",
                    pointer,
                    "only local $defs references are allowed",
                )
            )

        node_type = node.get("type")
        is_object = (
            node_type == "object"
            or (isinstance(node_type, list) and "object" in node_type)
            or "properties" in node
        )
        if not is_object:
            continue
        if node.get("additionalProperties") is not False:
            problems.append(
                issue(
                    "schema.object_not_closed",
                    pointer,
                    "object schemas must set additionalProperties to false",
                )
            )
        if "patternProperties" in node:
            problems.append(
                issue(
                    "schema.pattern_properties_forbidden",
                    pointer,
                    "patternProperties is not supported by strict output contracts",
                )
            )
        if complete_required:
            properties = node.get("properties")
            required = node.get("required")
            if isinstance(properties, dict):
                property_names = set(properties)
                required_names: set[str] = set()
                if isinstance(required, list):
                    required_names = {name for name in required if isinstance(name, str)}
                if required_names != property_names:
                    problems.append(
                        issue(
                            "schema.required_incomplete",
                            pointer,
                            "required must list every property in strict output schemas",
                        )
                    )
    return tuple(problems)


def _walk_schema(value: JsonValue, path: str) -> Iterator[tuple[str, JsonObject]]:
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from _walk_schema(child, f"{path}/{escaped}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_schema(child, f"{path}/{index}")
