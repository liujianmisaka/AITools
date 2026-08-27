from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from misaka_coordinator_service.domain._serialization import (
    ensure_optional_text,
    ensure_text,
    read_mapping_list,
    read_optional_mapping,
    read_optional_text,
    read_text,
)
from misaka_coordinator_service.domain.errors import CoordinatorDomainError
from misaka_coordinator_service.domain.models import AgentSelection, TaskIntent


class CoordinatorDecisionKind(StrEnum):
    CREATE_PLAN = "create_plan"
    REVISE_PLAN = "revise_plan"
    DELEGATE = "delegate"
    DISPATCH_READY = "dispatch_ready_nodes"
    SEND_MESSAGE = "send_message"
    CANCEL_DELEGATION = "cancel_delegation"
    WAIT = "wait"
    REVIEW = "review"
    ACCEPT_RESULT = "accept_result"
    RESPOND = "respond"
    REQUEST_INPUT = "request_input"
    COMPLETE_GOAL = "complete_goal"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class CoordinatorDecision:
    decision_id: str
    kind: CoordinatorDecisionKind
    rationale: str
    tasks: tuple[TaskIntent, ...]
    selection: AgentSelection | None
    target_node_id: str | None
    message: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", ensure_text(self.decision_id, "decision_id"))
        object.__setattr__(self, "rationale", ensure_text(self.rationale, "rationale"))
        object.__setattr__(
            self,
            "target_node_id",
            ensure_optional_text(self.target_node_id, "target_node_id"),
        )
        object.__setattr__(self, "message", ensure_optional_text(self.message, "message"))
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise CoordinatorDomainError("decision task_id values must be unique")
        if (
            self.kind
            in {
                CoordinatorDecisionKind.CREATE_PLAN,
                CoordinatorDecisionKind.REVISE_PLAN,
            }
            and not self.tasks
        ):
            raise CoordinatorDomainError(f"{self.kind} decision requires tasks")
        if self.kind is CoordinatorDecisionKind.DELEGATE and (
            len(self.tasks) != 1 or self.selection is None
        ):
            raise CoordinatorDomainError("delegate decision requires one task and a selection")
        if self.kind is CoordinatorDecisionKind.REVIEW and self.target_node_id is None:
            raise CoordinatorDomainError("review decision requires target_node_id")
        if self.kind is CoordinatorDecisionKind.ACCEPT_RESULT and self.target_node_id is None:
            raise CoordinatorDomainError("accept_result decision requires target_node_id")
        if self.kind in {
            CoordinatorDecisionKind.SEND_MESSAGE,
            CoordinatorDecisionKind.CANCEL_DELEGATION,
        } and (self.target_node_id is None or self.message is None):
            raise CoordinatorDomainError(
                f"{self.kind} decision requires target_node_id and message"
            )
        if (
            self.kind
            in {
                CoordinatorDecisionKind.RESPOND,
                CoordinatorDecisionKind.REQUEST_INPUT,
            }
            and self.message is None
        ):
            raise CoordinatorDomainError(f"{self.kind} decision requires message")

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "kind": self.kind.value,
            "rationale": self.rationale,
            "tasks": [task.to_dict() for task in self.tasks],
            "selection": None if self.selection is None else self.selection.to_dict(),
            "target_node_id": self.target_node_id,
            "message": self.message,
        }

    @classmethod
    def from_value(cls, value: object) -> CoordinatorDecision:
        if not isinstance(value, dict):
            raise CoordinatorDomainError("coordinator decision must be a JSON object")
        raw = cast(dict[object, object], value)
        if any(not isinstance(key, str) for key in raw):
            raise CoordinatorDomainError("coordinator decision keys must be strings")
        data = cast(dict[str, object], raw)
        selection = read_optional_mapping(data, "selection")
        return cls(
            decision_id=read_text(data, "decision_id"),
            kind=CoordinatorDecisionKind(read_text(data, "kind")),
            rationale=read_text(data, "rationale"),
            tasks=tuple(TaskIntent.from_dict(task) for task in read_mapping_list(data, "tasks")),
            selection=None if selection is None else AgentSelection.from_dict(selection),
            target_node_id=read_optional_text(data, "target_node_id"),
            message=read_optional_text(data, "message"),
        )


TASK_INTENT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_id": {"type": "string", "minLength": 1},
        "objective": {"type": "string", "minLength": 1},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "required_capabilities": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "parent_task_id": {"type": ["string", "null"]},
    },
    "required": [
        "task_id",
        "objective",
        "acceptance_criteria",
        "required_capabilities",
        "constraints",
        "parent_task_id",
    ],
}

AGENT_SELECTION_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "provider_id": {"type": "string", "minLength": 1},
        "model_id": {"type": "string", "minLength": 1},
        "effort": {"type": ["string", "null"]},
        "rationale": {"type": "string", "minLength": 1},
        "capability_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["provider_id", "model_id", "effort", "rationale", "capability_ids"],
}

COORDINATOR_DECISION_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision_id": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": [item.value for item in CoordinatorDecisionKind]},
        "rationale": {"type": "string", "minLength": 1},
        "tasks": {"type": "array", "items": TASK_INTENT_SCHEMA},
        "selection": {"anyOf": [AGENT_SELECTION_SCHEMA, {"type": "null"}]},
        "target_node_id": {"type": ["string", "null"]},
        "message": {"type": ["string", "null"]},
    },
    "required": [
        "decision_id",
        "kind",
        "rationale",
        "tasks",
        "selection",
        "target_node_id",
        "message",
    ],
}

COORDINATOR_DECISION_RESPONSE_FORMAT: Mapping[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "coordinator_decision",
        "strict": True,
        "schema": COORDINATOR_DECISION_SCHEMA,
    },
}
