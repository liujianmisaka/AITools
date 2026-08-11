from __future__ import annotations

from collections.abc import Iterable

from multi_agent.domain.errors import OrchestrationModelNotFoundError
from multi_agent.orchestration.contracts import OrchestrationModel


class OrchestrationModelRegistry:
    def __init__(self, models: Iterable[OrchestrationModel] = ()) -> None:
        self._models: dict[str, OrchestrationModel] = {}
        for model in models:
            self.register(model)

    def register(self, model: OrchestrationModel) -> None:
        if not model.kind:
            raise ValueError("orchestration model kind cannot be empty")
        if model.definition_schema_version < 1:
            raise ValueError(
                "orchestration model schema version must be positive"
            )
        if model.kind in self._models:
            raise ValueError(f"orchestration model already registered: {model.kind}")
        self._models[model.kind] = model

    def get(self, kind: str) -> OrchestrationModel:
        try:
            return self._models[kind]
        except KeyError as exc:
            raise OrchestrationModelNotFoundError(
                f"orchestration model not found: {kind}"
            ) from exc

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "kind": model.kind,
                "definition_schema_version": model.definition_schema_version,
            }
            for model in sorted(self._models.values(), key=lambda item: item.kind)
        ]
