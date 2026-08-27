from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import cast

from misaka_coordinator_service.domain._serialization import (
    datetime_to_text,
    ensure_not_before,
    ensure_optional_text,
    ensure_text,
    ensure_utc,
    read_datetime,
    read_int,
    read_mapping,
    read_mapping_list,
    read_optional_mapping,
    read_optional_text,
    read_text,
)
from misaka_coordinator_service.domain.autonomy import CoordinatorAutonomyState
from misaka_coordinator_service.domain.errors import (
    CoordinatorDomainError,
    InvalidTransitionError,
)
from misaka_coordinator_service.domain.models import (
    CoordinatorEvent,
    ExecutionEventCursor,
    Goal,
    GoalStatus,
    Plan,
    PlanStatus,
)
from misaka_coordinator_service.domain.planning import PlanGraph

SESSION_SCHEMA_VERSION = 2
_LEGACY_SESSION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CoordinatorSession:
    session_id: str
    cognitive_session_id: str
    goal: Goal | None
    plan: Plan | None
    last_event_id: str | None
    last_event_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime
    plan_graph: PlanGraph | None = None
    event_cursors: tuple[ExecutionEventCursor, ...] = ()
    autonomy: CoordinatorAutonomyState = field(default_factory=CoordinatorAutonomyState)

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", ensure_text(self.session_id, "session_id"))
        object.__setattr__(
            self,
            "cognitive_session_id",
            ensure_text(self.cognitive_session_id, "cognitive_session_id"),
        )
        object.__setattr__(
            self,
            "last_event_id",
            ensure_optional_text(self.last_event_id, "last_event_id"),
        )
        if self.revision < 0:
            raise CoordinatorDomainError("revision must not be negative")
        cursor_ids = tuple(cursor.delegation_id for cursor in self.event_cursors)
        if len(cursor_ids) != len(set(cursor_ids)):
            raise CoordinatorDomainError("event cursor delegation_id values must be unique")
        created_at = ensure_utc(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)
        updated_at = ensure_not_before(self.updated_at, created_at, "updated_at")
        object.__setattr__(self, "updated_at", updated_at)
        if (self.last_event_id is None) != (self.last_event_at is None):
            raise CoordinatorDomainError("last_event_id and last_event_at must be set together")
        if self.last_event_at is not None:
            object.__setattr__(
                self,
                "last_event_at",
                ensure_not_before(self.last_event_at, created_at, "last_event_at"),
            )
        if self.plan is not None:
            if self.goal is None:
                raise CoordinatorDomainError("plan requires a goal")
            if self.plan.goal_id != self.goal.goal_id:
                raise CoordinatorDomainError("plan goal_id must match the session goal")
        if self.plan_graph is not None:
            if self.plan is None:
                raise CoordinatorDomainError("plan graph requires a plan")
            self.plan_graph.validate(self.plan)

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        cognitive_session_id: str,
        at: datetime,
    ) -> CoordinatorSession:
        return cls(
            session_id=session_id,
            cognitive_session_id=cognitive_session_id,
            goal=None,
            plan=None,
            last_event_id=None,
            last_event_at=None,
            revision=0,
            created_at=at,
            updated_at=at,
        )

    def event_cursor_for(self, delegation_id: str) -> ExecutionEventCursor:
        normalized_delegation_id = ensure_text(delegation_id, "delegation_id")
        for cursor in self.event_cursors:
            if cursor.delegation_id == normalized_delegation_id:
                return cursor
        return ExecutionEventCursor(delegation_id=normalized_delegation_id)

    def advance_event_cursor(
        self,
        delegation_id: str,
        sequence: int,
        *,
        at: datetime,
    ) -> CoordinatorSession:
        normalized_delegation_id = ensure_text(delegation_id, "delegation_id")
        current = self.event_cursor_for(normalized_delegation_id)
        advanced = current.advance(sequence)
        if advanced is current:
            return self
        cursors = [
            advanced if cursor.delegation_id == normalized_delegation_id else cursor
            for cursor in self.event_cursors
        ]
        if all(cursor.delegation_id != normalized_delegation_id for cursor in self.event_cursors):
            cursors.append(advanced)
        return replace(
            self,
            event_cursors=tuple(cursors),
            revision=self.revision + 1,
            updated_at=self._next_time(at),
        )

    def start_goal(self, goal: Goal, *, at: datetime) -> CoordinatorSession:
        if goal.status is not GoalStatus.ACTIVE:
            raise CoordinatorDomainError("new session goal must be active")
        if self.goal is not None and self.goal.status is GoalStatus.ACTIVE:
            raise InvalidTransitionError("session already has an active goal")
        return replace(
            self,
            goal=goal,
            plan=None,
            plan_graph=None,
            event_cursors=(),
            autonomy=CoordinatorAutonomyState(),
            revision=self.revision + 1,
            updated_at=ensure_not_before(at, max(self.updated_at, goal.updated_at), "at"),
        )

    def update_autonomy(
        self, autonomy: CoordinatorAutonomyState, *, at: datetime
    ) -> CoordinatorSession:
        if autonomy == self.autonomy:
            return self
        return replace(
            self,
            autonomy=autonomy,
            revision=self.revision + 1,
            updated_at=self._next_time(at),
        )

    def attach_plan(self, plan: Plan, *, at: datetime) -> CoordinatorSession:
        if self.goal is None:
            raise CoordinatorDomainError("session requires a goal before attaching a plan")
        if self.goal.status is not GoalStatus.ACTIVE:
            raise InvalidTransitionError("cannot attach a plan to an inactive goal")
        if plan.goal_id != self.goal.goal_id:
            raise CoordinatorDomainError("plan goal_id must match the session goal")
        if self.plan is not None:
            if self.plan.plan_id != plan.plan_id:
                raise InvalidTransitionError("session already has a different plan")
            if self.plan == plan:
                return self
            if plan.revision <= self.plan.revision:
                raise CoordinatorDomainError("plan revision must move forward")
        return replace(
            self,
            plan=plan,
            revision=self.revision + 1,
            updated_at=ensure_not_before(at, max(self.updated_at, plan.updated_at), "at"),
        )

    def attach_plan_graph(self, plan_graph: PlanGraph, *, at: datetime) -> CoordinatorSession:
        if self.plan is None:
            raise CoordinatorDomainError("session requires a plan before attaching a plan graph")
        if self.goal is None or self.goal.status is not GoalStatus.ACTIVE:
            raise InvalidTransitionError("cannot attach a plan graph to an inactive goal")
        plan_graph.validate(self.plan)
        if self.plan_graph is not None:
            if self.plan_graph == plan_graph:
                return self
            if plan_graph.revision <= self.plan_graph.revision:
                raise CoordinatorDomainError("plan graph revision must move forward")
        return replace(
            self,
            plan_graph=plan_graph,
            revision=self.revision + 1,
            updated_at=ensure_not_before(at, max(self.updated_at, plan_graph.updated_at), "at"),
        )

    def record_event(self, event: CoordinatorEvent, *, at: datetime) -> CoordinatorSession:
        if event.session_id != self.session_id:
            raise CoordinatorDomainError("event session_id must match the session")
        if self.last_event_id == event.event_id:
            return self
        if self.last_event_at is not None and event.occurred_at < self.last_event_at:
            raise CoordinatorDomainError("event cursor must not move backwards")
        processed_at = ensure_not_before(at, event.occurred_at, "at")
        return replace(
            self,
            last_event_id=event.event_id,
            last_event_at=event.occurred_at,
            revision=self.revision + 1,
            updated_at=self._next_time(processed_at),
        )

    def record_external_event(
        self,
        event: CoordinatorEvent,
        *,
        delegation_id: str,
        sequence: int,
        at: datetime,
    ) -> CoordinatorSession:
        """Record one ordered V3 event and advance only that delegation's cursor."""

        if event.session_id != self.session_id:
            raise CoordinatorDomainError("event session_id must match the session")
        normalized_delegation_id = ensure_text(delegation_id, "delegation_id")
        if event.execution is not None and (
            event.execution.delegation_id != normalized_delegation_id
        ):
            raise CoordinatorDomainError("event execution delegation_id must match the cursor")
        current_cursor = self.event_cursor_for(normalized_delegation_id)
        advanced_cursor = current_cursor.advance(sequence)
        if advanced_cursor is current_cursor:
            return self
        cursors = [
            advanced_cursor if cursor.delegation_id == normalized_delegation_id else cursor
            for cursor in self.event_cursors
        ]
        if all(cursor.delegation_id != normalized_delegation_id for cursor in self.event_cursors):
            cursors.append(advanced_cursor)
        event_at = ensure_not_before(event.occurred_at, self.created_at, "event.occurred_at")
        latest_event_at = (
            event_at if self.last_event_at is None else max(self.last_event_at, event_at)
        )
        return replace(
            self,
            last_event_id=event.event_id,
            last_event_at=latest_event_at,
            event_cursors=tuple(cursors),
            revision=self.revision + 1,
            updated_at=self._next_time(at),
        )

    def complete_goal(self, *, at: datetime) -> CoordinatorSession:
        if self.goal is None or self.goal.status is not GoalStatus.ACTIVE:
            raise InvalidTransitionError("session does not have an active goal")
        if self.plan is None or self.plan.status is not PlanStatus.COMPLETED:
            raise CoordinatorDomainError("goal completion requires a completed plan")
        return replace(
            self,
            goal=self.goal.transition(GoalStatus.COMPLETED, at=at),
            revision=self.revision + 1,
            updated_at=self._next_time(at),
        )

    def fail_goal(self, *, at: datetime) -> CoordinatorSession:
        if self.goal is None or self.goal.status is not GoalStatus.ACTIVE:
            raise InvalidTransitionError("session does not have an active goal")
        return replace(
            self,
            goal=self.goal.transition(GoalStatus.FAILED, at=at),
            revision=self.revision + 1,
            updated_at=self._next_time(at),
        )

    def cancel_goal(self, *, at: datetime) -> CoordinatorSession:
        if self.goal is None or self.goal.status is not GoalStatus.ACTIVE:
            raise InvalidTransitionError("session does not have an active goal")
        return replace(
            self,
            goal=self.goal.transition(GoalStatus.CANCELLED, at=at),
            revision=self.revision + 1,
            updated_at=self._next_time(at),
        )

    def _next_time(self, at: datetime) -> datetime:
        return ensure_not_before(at, self.updated_at, "at")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "cognitive_session_id": self.cognitive_session_id,
            "goal": None if self.goal is None else self.goal.to_dict(),
            "plan": None if self.plan is None else self.plan.to_dict(),
            "last_event_id": self.last_event_id,
            "last_event_at": (
                None if self.last_event_at is None else datetime_to_text(self.last_event_at)
            ),
            "revision": self.revision,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
            "autonomy": self.autonomy.to_dict(),
        }
        if self.plan_graph is not None:
            payload["plan_graph"] = self.plan_graph.to_dict()
        if self.event_cursors:
            payload["event_cursors"] = [cursor.to_dict() for cursor in self.event_cursors]
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CoordinatorSession:
        schema_version = read_int(data, "schema_version")
        if schema_version not in {_LEGACY_SESSION_SCHEMA_VERSION, SESSION_SCHEMA_VERSION}:
            raise CoordinatorDomainError(
                f"unsupported coordinator session schema_version {schema_version}"
            )
        goal = read_optional_mapping(data, "goal")
        plan = read_optional_mapping(data, "plan")
        plan_graph = read_optional_mapping(data, "plan_graph")
        event_cursors = ()
        if data.get("event_cursors") is not None:
            event_cursors = tuple(
                ExecutionEventCursor.from_dict(item)
                for item in read_mapping_list(data, "event_cursors")
            )
        last_event_at_raw = data.get("last_event_at")
        last_event_at = None
        if last_event_at_raw is not None:
            last_event_at = read_datetime(data, "last_event_at")
        autonomy = CoordinatorAutonomyState()
        if schema_version == SESSION_SCHEMA_VERSION:
            autonomy = CoordinatorAutonomyState.from_dict(read_mapping(data, "autonomy"))
        return cls(
            session_id=read_text(data, "session_id"),
            cognitive_session_id=read_text(data, "cognitive_session_id"),
            goal=None if goal is None else Goal.from_dict(goal),
            plan=None if plan is None else Plan.from_dict(plan),
            plan_graph=None if plan_graph is None else PlanGraph.from_dict(plan_graph),
            last_event_id=read_optional_text(data, "last_event_id"),
            last_event_at=last_event_at,
            revision=read_int(data, "revision"),
            created_at=read_datetime(data, "created_at"),
            updated_at=read_datetime(data, "updated_at"),
            event_cursors=event_cursors,
            autonomy=autonomy,
        )


def dump_session(session: CoordinatorSession) -> str:
    return json.dumps(session.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def load_session(payload: str) -> CoordinatorSession:
    try:
        decoded = cast(object, json.loads(payload))
    except json.JSONDecodeError as error:
        raise CoordinatorDomainError("coordinator session payload must be valid JSON") from error
    if not isinstance(decoded, dict):
        raise CoordinatorDomainError("coordinator session payload must be a JSON object")
    raw = cast(dict[object, object], decoded)
    if any(not isinstance(key, str) for key in raw):
        raise CoordinatorDomainError("coordinator session payload must be a JSON object")
    return CoordinatorSession.from_dict(cast(dict[str, object], raw))
