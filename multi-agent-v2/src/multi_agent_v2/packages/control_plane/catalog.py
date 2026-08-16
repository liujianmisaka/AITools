from __future__ import annotations

from multi_agent_v2.packages.persistence import ControlPlaneRepository
from multi_agent_v2.packages.workflow_dsl import (
    CompilationContext,
    ProviderModel,
    RegisteredActivity,
)
from multi_agent_v2.packages.workflow_dsl.canonical import canonical_json, sha256_text


class DatabaseWorkflowCatalog:
    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        workspace_ids: tuple[str, ...],
        activities: tuple[RegisteredActivity, ...] = (),
    ) -> None:
        self._repository = repository
        self._workspace_ids = tuple(sorted(workspace_ids))
        self._activities = tuple(sorted(activities, key=lambda item: (item.name, item.version)))

    async def compilation_context(self) -> CompilationContext:
        catalog = await self._repository.get_provider_catalog("codex")
        provider_models = tuple(
            ProviderModel(
                provider=catalog.runtime_name,
                model=model.id,
                efforts=model.efforts,
            )
            for model in catalog.models
        )
        revision = sha256_text(
            canonical_json(
                {
                    "providerCatalogRevision": catalog.revision,
                    "workspaceIds": list(self._workspace_ids),
                    "activities": [
                        {
                            "name": item.name,
                            "version": item.version,
                            "outputSchema": item.output_schema,
                        }
                        for item in self._activities
                    ],
                }
            )
        )
        return CompilationContext(
            catalog_revision=revision,
            provider_models=provider_models,
            workspace_ids=self._workspace_ids,
            activities=self._activities,
        )

    def workspace_ids(self) -> tuple[str, ...]:
        return self._workspace_ids
