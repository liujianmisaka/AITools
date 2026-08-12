from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from multi_agent.domain.errors import EventContractError, EventTypeNotFoundError
from multi_agent.domain.models import TriggerEventInput


class GitCommitUpdatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    remote: str
    branch: str
    before_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    after_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    update_kind: str = Field(pattern=r"^(forward|rewritten)$")
    commit_count: int | None = Field(default=None, ge=1)
    subject: str
    author_name: str
    authored_at: datetime
    observed_at: datetime


class UnrestrictedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True, slots=True)
class EventTypeDefinition:
    event_type: str
    version: int
    description: str
    source_types: tuple[str, ...]
    payload_model: type[BaseModel]

    def describe(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "version": self.version,
            "description": self.description,
            "source_types": list(self.source_types),
            "payload_schema": self.payload_model.model_json_schema(),
        }


class EventTypeRegistry:
    def __init__(self, definitions: Iterable[EventTypeDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, int], EventTypeDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: EventTypeDefinition) -> None:
        key = (definition.event_type, definition.version)
        if key in self._definitions:
            raise ValueError(
                "event type already registered: "
                f"{definition.event_type}@{definition.version}"
            )
        if not definition.source_types:
            raise ValueError("event type must allow at least one source type")
        self._definitions[key] = definition

    def get(self, event_type: str, version: int) -> EventTypeDefinition:
        try:
            return self._definitions[(event_type, version)]
        except KeyError as exc:
            raise EventTypeNotFoundError(
                f"event type not registered: {event_type}@{version}"
            ) from exc

    def validate_event(self, event: TriggerEventInput) -> TriggerEventInput:
        definition = self.get(event.event_type, event.event_version)
        if event.source_type not in definition.source_types:
            raise EventContractError(
                f"event {event.event_type}@{event.event_version} does not allow "
                f"source type {event.source_type!r}"
            )
        try:
            payload = definition.payload_model.model_validate(event.payload)
        except ValidationError as exc:
            raise EventContractError(
                f"event payload violates {event.event_type}@{event.event_version}: "
                f"{exc}"
            ) from exc
        return event.model_copy(update={"payload": payload.model_dump(mode="json")})

    def validate_binding(
        self,
        *,
        source_type: str,
        event_type: str,
        event_version: int,
    ) -> None:
        definition = self.get(event_type, event_version)
        if source_type not in definition.source_types:
            raise EventContractError(
                f"event {event_type}@{event_version} does not allow source type "
                f"{source_type!r}"
            )

    def describe(self) -> list[dict[str, Any]]:
        return [
            definition.describe()
            for definition in sorted(
                self._definitions.values(),
                key=lambda item: (item.event_type, item.version),
            )
        ]


def default_event_type_registry() -> EventTypeRegistry:
    return EventTypeRegistry(
        [
            EventTypeDefinition(
                event_type="git.commit.updated",
                version=1,
                description=(
                    "A configured remote branch head changed after a Git poll."
                ),
                source_types=("git_commit",),
                payload_model=GitCommitUpdatedPayload,
            ),
            EventTypeDefinition(
                event_type="manual.event",
                version=1,
                description="A manually submitted event with an open payload.",
                source_types=("manual",),
                payload_model=UnrestrictedPayload,
            ),
        ]
    )
