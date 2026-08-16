from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

import yaml
from pydantic import ValidationError
from yaml.events import AliasEvent, NodeEvent
from yaml.nodes import MappingNode, Node

from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue
from multi_agent_v2.packages.workflow_dsl.errors import WorkflowCompilationError, issue
from multi_agent_v2.packages.workflow_dsl.models import WorkflowDefinition

MAX_DOCUMENT_BYTES = 1_048_576
MAX_DOCUMENT_DEPTH = 64
_NODE_TAGS = {"agent", "activity", "decision", "approval", "timer", "join"}
_FLOW_TAGS = {"dag", "state_machine"}


class _DocumentError(ValueError):
    pass


class _StrictSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent: Node | None, index: int) -> Node | None:
        if self.check_event(AliasEvent):
            raise _DocumentError("YAML aliases are forbidden")
        event = cast(NodeEvent, self.peek_event())  # pyright: ignore[reportUnknownMemberType]
        if event.anchor is not None:
            raise _DocumentError("YAML anchors are forbidden")
        return super().compose_node(parent, index)


def _construct_mapping(
    loader: _StrictSafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[str, object]:
    mapping: dict[str, object] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge" or key_node.value == "<<":
            raise _DocumentError("YAML merge keys are forbidden")
        key = cast(
            object,
            loader.construct_object(key_node, deep=deep),  # pyright: ignore[reportUnknownMemberType]
        )
        if not isinstance(key, str):
            raise _DocumentError("YAML object keys must be strings")
        if key in mapping:
            raise _DocumentError(f"duplicate key: {key}")
        mapping[key] = cast(
            object,
            loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
                value_node,
                deep=deep,
            ),
        )
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _reject_duplicate_pairs(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DocumentError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _DocumentError(f"non-finite JSON number is forbidden: {value}")


def _check_document_size(document: str | bytes) -> str:
    raw = document.encode("utf-8") if isinstance(document, str) else document
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise WorkflowCompilationError(
            [issue("document.too_large", "", "workflow document exceeds size limit")]
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowCompilationError(
            [issue("document.invalid_encoding", "", "workflow document must be UTF-8")]
        ) from exc


def _check_depth(value: JsonValue, *, depth: int = 0) -> None:
    if depth > MAX_DOCUMENT_DEPTH:
        raise WorkflowCompilationError(
            [issue("document.too_deep", "", "workflow document exceeds nesting limit")]
        )
    if isinstance(value, dict):
        for child in value.values():
            _check_depth(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_depth(child, depth=depth + 1)


def _json_pointer(location: tuple[str | int, ...]) -> str:
    if not location:
        return ""
    escaped = (str(part).replace("~", "~0").replace("/", "~1") for part in location)
    return "/" + "/".join(escaped)


def _external_location(location: tuple[str | int, ...]) -> tuple[str | int, ...]:
    result: list[str | int] = []
    for index, part in enumerate(location):
        previous = location[index - 1] if index > 0 else None
        if isinstance(part, str) and part in _NODE_TAGS and isinstance(previous, int):
            continue
        if isinstance(part, str) and part in _FLOW_TAGS and previous == "flow":
            continue
        result.append(part)
    return tuple(result)


def _validate(raw: object) -> WorkflowDefinition:
    if not isinstance(raw, dict):
        raise WorkflowCompilationError(
            [issue("document.root_not_object", "", "workflow document root must be an object")]
        )
    json_object = cast(JsonObject, raw)
    _check_depth(json_object)
    try:
        return WorkflowDefinition.model_validate(json_object)
    except ValidationError as exc:
        issues = [
            issue(
                "dsl.invalid",
                _json_pointer(_external_location(error["loc"])),
                error["msg"],
                error_type=error["type"],
            )
            for error in exc.errors(include_url=False, include_input=False)
        ]
        raise WorkflowCompilationError(issues) from exc


def parse_json_workflow(document: str | bytes) -> WorkflowDefinition:
    text = _check_document_size(document)
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, _DocumentError) as exc:
        raise WorkflowCompilationError([issue("document.invalid_json", "", str(exc))]) from exc
    return _validate(raw)


def parse_yaml_workflow(document: str | bytes) -> WorkflowDefinition:
    text = _check_document_size(document)
    try:
        raw: Any = yaml.load(text, Loader=_StrictSafeLoader)
    except (yaml.YAMLError, _DocumentError) as exc:
        raise WorkflowCompilationError([issue("document.invalid_yaml", "", str(exc))]) from exc
    return _validate(raw)
