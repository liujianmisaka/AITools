from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from temporalio.client import ScheduleUpdate, ScheduleUpdateInput

from multi_agent_v2.apps.command_dispatcher.dispatcher import (
    CommandDispatcher,
    CommandTransport,
    TemporalCommandTransport,
)
from multi_agent_v2.packages.control_plane.models import OutboxCommand, ScheduleRecord
from multi_agent_v2.packages.persistence import ControlPlaneRepository, OutboxFailure
from multi_agent_v2.packages.workflow_runtime.temporal import TemporalGateway


def _command(index: int, *, command_type: str = "test.v1") -> OutboxCommand:
    return OutboxCommand(
        outbox_id=f"outbox-{index}",
        command_id=f"command-{index}",
        command_type=command_type,
        aggregate_type="test",
        aggregate_id=f"aggregate-{index}",
        payload={},
        attempts=1,
        lease_owner="dispatcher-1",
        lease_epoch=1,
    )


class _DispatcherRepository:
    def __init__(
        self,
        commands: tuple[OutboxCommand, ...],
        *,
        renew_error: Exception | None = None,
    ) -> None:
        self.commands = commands
        self.renew_error = renew_error
        self.completed: list[str] = []
        self.failed: list[str] = []
        self.renewed: list[str] = []

    async def claim_outbox(
        self,
        *,
        lease_owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[OutboxCommand, ...]:
        assert lease_owner == "dispatcher-1"
        assert lease_duration > timedelta(0)
        return self.commands[:limit]

    async def renew_outbox(
        self,
        command: OutboxCommand,
        *,
        lease_duration: timedelta,
    ) -> bool:
        self.renewed.append(command.command_id)
        if self.renew_error is not None:
            raise self.renew_error
        return True

    async def complete_outbox(self, command: OutboxCommand) -> None:
        self.completed.append(command.command_id)

    async def fail_outbox(
        self,
        command: OutboxCommand,
        *,
        error: str,
        retry_delay: timedelta,
        maximum_attempts: int,
    ) -> OutboxFailure:
        self.failed.append(command.command_id)
        return OutboxFailure(
            status="failed",
            attempts=command.attempts,
            available_at=datetime.now(UTC) + retry_delay,
        )


class _ParallelTransport:
    def __init__(self, expected: int, *, delay: float = 0.01) -> None:
        self.expected = expected
        self.delay = delay
        self.active = 0
        self.maximum_active = 0
        self.all_started = asyncio.Event()

    async def dispatch(self, command: OutboxCommand) -> None:
        del command
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active == self.expected:
            self.all_started.set()
        await asyncio.wait_for(self.all_started.wait(), timeout=1)
        await asyncio.sleep(self.delay)
        self.active -= 1


async def test_dispatcher_starts_renewal_for_every_claimed_command() -> None:
    commands = (_command(1), _command(2))
    repository = _DispatcherRepository(commands)
    transport = _ParallelTransport(len(commands), delay=0.02)
    dispatcher = CommandDispatcher(
        repository=cast(ControlPlaneRepository, repository),
        transport=cast(CommandTransport, transport),
        lease_owner="dispatcher-1",
        lease_duration=timedelta(milliseconds=9),
    )

    count = await dispatcher.run_once()

    assert count == 2
    assert transport.maximum_active == 2
    assert set(repository.renewed) == {"command-1", "command-2"}
    assert repository.completed == ["command-1", "command-2"]


async def test_dispatcher_does_not_finalize_after_lease_renewal_failure() -> None:
    repository = _DispatcherRepository(
        (_command(1),),
        renew_error=RuntimeError("database unavailable"),
    )
    transport = _ParallelTransport(1, delay=0.1)
    dispatcher = CommandDispatcher(
        repository=cast(ControlPlaneRepository, repository),
        transport=cast(CommandTransport, transport),
        lease_owner="dispatcher-1",
        lease_duration=timedelta(milliseconds=30),
    )

    assert await dispatcher.run_once() == 1
    assert repository.renewed == ["command-1"]
    assert repository.completed == []
    assert repository.failed == []


class _ScheduleHandle:
    def __init__(self, note: str) -> None:
        self.note = note
        self.updates: list[ScheduleUpdate | None] = []

    async def describe(self) -> object:
        return object()

    async def update(
        self,
        updater: Callable[
            [ScheduleUpdateInput],
            ScheduleUpdate | Awaitable[ScheduleUpdate | None] | None,
        ],
    ) -> None:
        result = updater(
            cast(
                ScheduleUpdateInput,
                SimpleNamespace(
                    description=SimpleNamespace(
                        schedule=SimpleNamespace(
                            state=SimpleNamespace(note=self.note),
                        )
                    )
                ),
            )
        )
        if isinstance(result, Awaitable):
            result = await result
        self.updates.append(result)


class _ScheduleClient:
    def __init__(self, handle: _ScheduleHandle) -> None:
        self.handle = handle

    def get_schedule_handle(self, schedule_id: str) -> _ScheduleHandle:
        assert schedule_id == "nightly"
        return self.handle


class _ScheduleGateway:
    def __init__(self, client: _ScheduleClient) -> None:
        self.client = client

    async def connect(self) -> _ScheduleClient:
        return self.client


def _schedule_command(revision: int) -> OutboxCommand:
    now = datetime.now(UTC)
    record = ScheduleRecord(
        schedule_id="nightly",
        name="Nightly",
        revision=revision,
        enabled=True,
        schedule_kind="cron",
        schedule_spec={"expressions": ["0 1 * * *"]},
        target_kind="workflow",
        target={
            "templateId": "addition",
            "templateVersion": 1,
            "workflowInput": {},
        },
        created_at=now,
        updated_at=now,
    )
    return _command(1, command_type="schedule.sync.v1").model_copy(
        update={"payload": record.model_dump(mode="json", by_alias=True)}
    )


async def test_stale_schedule_command_cannot_overwrite_a_newer_revision() -> None:
    handle = _ScheduleHandle("managed by multi-agent-v2 revision 2")
    gateway = _ScheduleGateway(_ScheduleClient(handle))
    transport = TemporalCommandTransport(
        repository=cast(ControlPlaneRepository, object()),
        temporal=cast(TemporalGateway, gateway),
    )

    await transport.dispatch(_schedule_command(1))

    assert handle.updates == [None]


async def test_schedule_dispatch_refuses_to_take_over_an_unmanaged_schedule() -> None:
    handle = _ScheduleHandle("created manually")
    gateway = _ScheduleGateway(_ScheduleClient(handle))
    transport = TemporalCommandTransport(
        repository=cast(ControlPlaneRepository, object()),
        temporal=cast(TemporalGateway, gateway),
    )

    with pytest.raises(ValueError, match="not managed"):
        await transport.dispatch(_schedule_command(1))
