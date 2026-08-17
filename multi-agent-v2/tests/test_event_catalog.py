from __future__ import annotations

import ast
from pathlib import Path

from multi_agent_v2.packages.event_catalog import EVENT_CATALOG, render_catalog_json


def _string_keywords(
    path: Path,
    *,
    keyword: str,
    call_names: set[str] | None = None,
) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else node.func.id
            if isinstance(node.func, ast.Name)
            else None
        )
        if call_names is not None and call_name not in call_names:
            continue
        for item in node.keywords:
            if (
                item.arg == keyword
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            ):
                values.add(item.value.value)
    return values


def _workflow_message_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"signal", "update"}:
            continue
        for item in node.keywords:
            if (
                item.arg == "name"
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            ):
                values.add(item.value.value)
    return values


def test_event_catalog_is_unique_sorted_and_fresh() -> None:
    names = [descriptor.event_name for descriptor in EVENT_CATALOG]
    generated = Path(__file__).parents[1] / "docs" / "event-catalog.json"

    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert generated.read_text(encoding="utf-8") == render_catalog_json()


def test_agent_activity_evidence_literals_are_registered() -> None:
    activity_path = (
        Path(__file__).parents[1]
        / "src"
        / "multi_agent_v2"
        / "packages"
        / "agent_execution"
        / "activity.py"
    )
    tree = ast.parse(activity_path.read_text(encoding="utf-8"))
    literals: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "event_type":
            continue
        candidates = (
            (node.value.body, node.value.orelse)
            if isinstance(node.value, ast.IfExp)
            else (node.value,)
        )
        for candidate in candidates:
            if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                literals.add(candidate.value)
    registered = {descriptor.event_name for descriptor in EVENT_CATALOG}

    assert literals <= registered


def test_workflow_outbox_and_cloudevent_names_are_registered() -> None:
    source = Path(__file__).parents[1] / "src" / "multi_agent_v2"
    registered = {descriptor.event_name for descriptor in EVENT_CATALOG}
    workflow_messages = _workflow_message_names(
        source / "packages" / "workflow_runtime" / "workflow.py"
    )
    outbox_commands = _string_keywords(
        source / "packages" / "persistence" / "control_repository.py",
        keyword="command_type",
        call_names={"CommandOutbox"},
    )
    cloud_events: set[str] = set()
    for relative in (
        Path("packages/eventing/git_connector.py"),
        Path("packages/eventing/webhook.py"),
    ):
        cloud_events.update(
            _string_keywords(
                source / relative,
                keyword="type",
                call_names={"CloudEventEnvelope"},
            )
        )
    projection_events = _string_keywords(
        source / "packages" / "workflow_runtime" / "workflow.py",
        keyword="event_type",
    )

    assert workflow_messages <= registered
    assert outbox_commands <= registered
    assert cloud_events <= registered
    assert projection_events <= registered
