from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from multi_agent.domain.models import TaskSpec, WorkItemSeed


DefinitionT = TypeVar("DefinitionT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class OrchestrationRuntimeContext:
    instance_id: str
    store: Any
    execute_agent_task: Callable[[str, TaskSpec], Awaitable[None]]
    is_closing: Callable[[], bool]


class OrchestrationModel(ABC, Generic[DefinitionT]):
    kind: str
    definition_schema_version: int

    @abstractmethod
    def parse_definition(
        self,
        value: BaseModel | Mapping[str, Any],
    ) -> DefinitionT:
        raise NotImplementedError

    @abstractmethod
    def validate_definition(
        self,
        definition: DefinitionT,
        *,
        validate_agent_task: Callable[[TaskSpec], None],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def display_name(self, definition: DefinitionT) -> str:
        raise NotImplementedError

    @abstractmethod
    def definition_id(self, definition: DefinitionT) -> str:
        raise NotImplementedError

    @abstractmethod
    def definition_version(self, definition: DefinitionT) -> int:
        raise NotImplementedError

    @abstractmethod
    def with_definition_version(
        self,
        definition: DefinitionT,
        version: int,
    ) -> DefinitionT:
        raise NotImplementedError

    @abstractmethod
    def materialize_work_items(
        self,
        definition: DefinitionT,
    ) -> Sequence[WorkItemSeed]:
        raise NotImplementedError

    @abstractmethod
    async def run(
        self,
        definition: DefinitionT,
        context: OrchestrationRuntimeContext,
    ) -> None:
        raise NotImplementedError
