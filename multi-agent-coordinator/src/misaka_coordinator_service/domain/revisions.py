from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from misaka_coordinator_service.domain._serialization import (
    datetime_to_text,
    ensure_text,
    ensure_utc,
    read_datetime,
    read_int,
    read_mapping_list,
    read_text,
)
from misaka_coordinator_service.domain.errors import CoordinatorDomainError
from misaka_coordinator_service.domain.models import TaskIntent


@dataclass(frozen=True, slots=True)
class PlanRevision:
    plan_id: str
    revision: int
    objective: str
    rationale: str
    tasks: tuple[TaskIntent, ...]
    supersedes_revision: int | None
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", ensure_text(self.plan_id, "plan_id"))
        object.__setattr__(self, "objective", ensure_text(self.objective, "objective"))
        object.__setattr__(self, "rationale", ensure_text(self.rationale, "rationale"))
        if isinstance(self.revision, bool) or self.revision < 1:
            raise CoordinatorDomainError("plan revision must be positive")
        if self.supersedes_revision is not None and (
            isinstance(self.supersedes_revision, bool) or self.supersedes_revision < 1
        ):
            raise CoordinatorDomainError("supersedes_revision must be positive")
        if self.revision == 1 and self.supersedes_revision is not None:
            raise CoordinatorDomainError("first plan revision cannot supersede another revision")
        if self.revision > 1 and self.supersedes_revision != self.revision - 1:
            raise CoordinatorDomainError(
                "plan revision must supersede the immediately preceding revision"
            )
        task_ids = tuple(task.task_id for task in self.tasks)
        if not task_ids:
            raise CoordinatorDomainError("plan revision requires at least one task")
        if len(task_ids) != len(set(task_ids)):
            raise CoordinatorDomainError("plan revision task_id values must be unique")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, "created_at"))

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        objective: str,
        rationale: str,
        tasks: tuple[TaskIntent, ...],
        previous: PlanRevision | None,
        at: datetime,
    ) -> PlanRevision:
        return cls(
            plan_id=plan_id,
            revision=1 if previous is None else previous.revision + 1,
            objective=objective,
            rationale=rationale,
            tasks=tasks,
            supersedes_revision=None if previous is None else previous.revision,
            created_at=at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "revision": self.revision,
            "objective": self.objective,
            "rationale": self.rationale,
            "tasks": [task.to_dict() for task in self.tasks],
            "supersedes_revision": self.supersedes_revision,
            "created_at": datetime_to_text(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PlanRevision:
        supersedes_revision = data.get("supersedes_revision")
        if supersedes_revision is not None and (
            isinstance(supersedes_revision, bool) or not isinstance(supersedes_revision, int)
        ):
            raise CoordinatorDomainError("supersedes_revision must be an integer or null")
        return cls(
            plan_id=read_text(data, "plan_id"),
            revision=read_int(data, "revision"),
            objective=read_text(data, "objective"),
            rationale=read_text(data, "rationale"),
            tasks=tuple(TaskIntent.from_dict(task) for task in read_mapping_list(data, "tasks")),
            supersedes_revision=supersedes_revision,
            created_at=read_datetime(data, "created_at"),
        )
