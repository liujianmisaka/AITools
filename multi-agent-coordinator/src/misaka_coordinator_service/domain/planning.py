from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from misaka_coordinator_service.domain._serialization import (
    datetime_to_text,
    ensure_not_before,
    ensure_text,
    ensure_utc,
    read_datetime,
    read_int,
    read_mapping_list,
    read_text,
)
from misaka_coordinator_service.domain.errors import CoordinatorDomainError
from misaka_coordinator_service.domain.models import Plan, PlanNodeStatus


@dataclass(frozen=True, slots=True)
class PlanDependency:
    node_id: str
    depends_on_node_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", ensure_text(self.node_id, "node_id"))
        object.__setattr__(
            self,
            "depends_on_node_id",
            ensure_text(self.depends_on_node_id, "depends_on_node_id"),
        )
        if self.node_id == self.depends_on_node_id:
            raise CoordinatorDomainError("plan dependency cannot point to itself")

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "depends_on_node_id": self.depends_on_node_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PlanDependency:
        return cls(
            node_id=read_text(data, "node_id"),
            depends_on_node_id=read_text(data, "depends_on_node_id"),
        )


@dataclass(frozen=True, slots=True)
class PlanGraph:
    plan_id: str
    dependencies: tuple[PlanDependency, ...]
    revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", ensure_text(self.plan_id, "plan_id"))
        if self.revision < 0:
            raise CoordinatorDomainError("plan graph revision must not be negative")
        dependency_keys = tuple(
            (dependency.node_id, dependency.depends_on_node_id) for dependency in self.dependencies
        )
        if len(dependency_keys) != len(set(dependency_keys)):
            raise CoordinatorDomainError("plan graph dependencies must be unique")
        graph_node_ids = tuple(
            dict.fromkeys(
                node_id
                for dependency in self.dependencies
                for node_id in (dependency.node_id, dependency.depends_on_node_id)
            )
        )
        self._topological_order(graph_node_ids)
        created_at = ensure_utc(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(
            self,
            "updated_at",
            ensure_not_before(self.updated_at, created_at, "updated_at"),
        )

    @classmethod
    def empty(cls, *, plan_id: str, at: datetime) -> PlanGraph:
        return cls(
            plan_id=plan_id,
            dependencies=(),
            revision=0,
            created_at=at,
            updated_at=at,
        )

    def add_dependency(
        self,
        *,
        node_id: str,
        depends_on_node_id: str,
        at: datetime,
    ) -> PlanGraph:
        dependency = PlanDependency(
            node_id=node_id,
            depends_on_node_id=depends_on_node_id,
        )
        if dependency in self.dependencies:
            raise CoordinatorDomainError("plan dependency already exists")
        return replace(
            self,
            dependencies=(*self.dependencies, dependency),
            revision=self.revision + 1,
            updated_at=self._next_time(at),
        )

    def remove_dependency(
        self,
        *,
        node_id: str,
        depends_on_node_id: str,
        at: datetime,
    ) -> PlanGraph:
        dependency = PlanDependency(
            node_id=node_id,
            depends_on_node_id=depends_on_node_id,
        )
        if dependency not in self.dependencies:
            raise CoordinatorDomainError("unknown plan dependency")
        return replace(
            self,
            dependencies=tuple(item for item in self.dependencies if item != dependency),
            revision=self.revision + 1,
            updated_at=self._next_time(at),
        )

    def dependencies_for(self, node_id: str) -> tuple[str, ...]:
        normalized_node_id = ensure_text(node_id, "node_id")
        return tuple(
            dependency.depends_on_node_id
            for dependency in self.dependencies
            if dependency.node_id == normalized_node_id
        )

    def validate(self, plan: Plan) -> None:
        if plan.plan_id != self.plan_id:
            raise CoordinatorDomainError("plan graph plan_id must match the plan")
        node_ids = tuple(node.node_id for node in plan.nodes)
        known_node_ids = set(node_ids)
        for dependency in self.dependencies:
            if dependency.node_id not in known_node_ids:
                raise CoordinatorDomainError(
                    f"plan graph references unknown node {dependency.node_id}"
                )
            if dependency.depends_on_node_id not in known_node_ids:
                raise CoordinatorDomainError(
                    f"plan graph references unknown dependency node {dependency.depends_on_node_id}"
                )
        self._topological_order(node_ids)

    def topological_order(self, plan: Plan) -> tuple[str, ...]:
        self.validate(plan)
        return self._topological_order(tuple(node.node_id for node in plan.nodes))

    def ready_node_ids(self, plan: Plan) -> tuple[str, ...]:
        self.validate(plan)
        nodes_by_id = {node.node_id: node for node in plan.nodes}
        return tuple(
            node.node_id
            for node in plan.nodes
            if node.status is PlanNodeStatus.READY
            and all(
                nodes_by_id[dependency_id].status is PlanNodeStatus.ACCEPTED
                for dependency_id in self.dependencies_for(node.node_id)
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "dependencies": [dependency.to_dict() for dependency in self.dependencies],
            "revision": self.revision,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PlanGraph:
        return cls(
            plan_id=read_text(data, "plan_id"),
            dependencies=tuple(
                PlanDependency.from_dict(item) for item in read_mapping_list(data, "dependencies")
            ),
            revision=read_int(data, "revision"),
            created_at=read_datetime(data, "created_at"),
            updated_at=read_datetime(data, "updated_at"),
        )

    def _topological_order(self, node_ids: tuple[str, ...]) -> tuple[str, ...]:
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        indegree = dict.fromkeys(node_ids, 0)
        for dependency in self.dependencies:
            outgoing[dependency.depends_on_node_id].append(dependency.node_id)
            indegree[dependency.node_id] += 1
        ready = [node_id for node_id in node_ids if indegree[node_id] == 0]
        order: list[str] = []
        while ready:
            node_id = ready.pop(0)
            order.append(node_id)
            for child_id in outgoing[node_id]:
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(child_id)
        if len(order) != len(node_ids):
            raise CoordinatorDomainError("plan graph must not contain cycles")
        return tuple(order)

    def _next_time(self, at: datetime) -> datetime:
        return ensure_not_before(at, self.updated_at, "at")
