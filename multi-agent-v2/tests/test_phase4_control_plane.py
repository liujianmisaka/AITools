from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from multi_agent_v2.packages.control_plane.models import (
    ScheduleCreate,
    ScheduleRecord,
    TriggerCreate,
)
from multi_agent_v2.packages.control_plane.schedule_adapter import ScheduleContractError
from multi_agent_v2.packages.control_plane.service import (
    ControlPlaneService,
    StaticWorkflowCatalog,
    TriggerContractError,
)
from multi_agent_v2.packages.persistence import (
    ControlPlaneNotFound,
    ControlPlaneRepository,
)
from multi_agent_v2.packages.workflow_dsl import CompilationContext


class _ScheduleRepository:
    def __init__(self, *, version_exists: bool = True) -> None:
        self.version_exists = version_exists
        self.created: list[ScheduleCreate] = []

    async def get_template_version(self, template_id: str, version: int) -> object:
        if not self.version_exists:
            raise ControlPlaneNotFound("missing")
        return (template_id, version)

    async def create_schedule(
        self,
        command: ScheduleCreate,
        *,
        idempotency_key: str,
    ) -> ScheduleRecord:
        assert idempotency_key == "schedule-key"
        self.created.append(command)
        now = datetime.now(UTC)
        return ScheduleRecord(
            schedule_id=command.schedule_id,
            name=command.name,
            revision=1,
            enabled=command.enabled,
            schedule_kind=command.schedule_kind,
            schedule_spec=command.schedule_spec,
            target_kind=command.target_kind,
            target=command.target,
            created_at=now,
            updated_at=now,
        )


def _service(repository: _ScheduleRepository) -> ControlPlaneService:
    return ControlPlaneService(
        repository=cast(ControlPlaneRepository, repository),
        catalog=StaticWorkflowCatalog(
            CompilationContext(
                catalog_revision="phase4-test",
                provider_models=(),
                workspace_ids=("repo",),
                activities=(),
            )
        ),
    )


async def test_schedule_rejects_unknown_workspace_before_persistence() -> None:
    repository = _ScheduleRepository()
    service = _service(repository)

    with pytest.raises(ScheduleContractError, match="unknown workspace"):
        await service.create_schedule(
            ScheduleCreate(
                schedule_id="poll-main",
                name="Poll main",
                schedule_kind="interval",
                schedule_spec={"everySeconds": 60},
                target_kind="git_connector",
                target={
                    "connectorId": "repo-main",
                    "workspaceId": "unknown",
                    "remote": "origin",
                    "branch": "main",
                },
            ),
            idempotency_key="schedule-key",
        )

    assert repository.created == []


async def test_schedule_requires_existing_workflow_template_version() -> None:
    service = _service(_ScheduleRepository(version_exists=False))

    with pytest.raises(ControlPlaneNotFound):
        await service.create_schedule(
            ScheduleCreate(
                schedule_id="nightly",
                name="Nightly",
                schedule_kind="cron",
                schedule_spec={"expressions": ["0 1 * * *"]},
                target_kind="workflow",
                target={
                    "templateId": "addition",
                    "templateVersion": 1,
                    "workflowInput": {},
                },
            ),
            idempotency_key="schedule-key",
        )


async def test_trigger_rejects_invalid_binding_before_persistence() -> None:
    service = _service(_ScheduleRepository())

    with pytest.raises(TriggerContractError):
        await service.create_trigger(
            TriggerCreate(
                trigger_id="on-build",
                name="On build",
                event_type="dev.misaka.webhook.received.v1",
                template_id="addition",
                template_version=1,
                input_bindings={"value": "[invalid"},
            ),
            idempotency_key="trigger-key",
        )
