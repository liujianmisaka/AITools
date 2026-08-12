from __future__ import annotations

import asyncio
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from multi_agent.domain.models import (
    ScheduledTaskDefinition,
    ScheduledTaskRunStatus,
    utc_now,
)
from multi_agent.scheduling.drivers import (
    CronScheduleDriver,
    PollTriggerBindingActionDriver,
    ScheduleDriverRegistry,
    ScheduledActionRegistry,
)
from multi_agent.storage.sqlite import SQLiteStore
from multi_agent.triggers.service import TriggerService


class PersistentSchedulerService:
    """Rebuilds in-memory timers from durable application-owned definitions."""

    _JOB_PREFIX = "scheduled-task:"

    def __init__(
        self,
        *,
        store: SQLiteStore,
        triggers: TriggerService,
        schedules: ScheduleDriverRegistry | None = None,
        actions: ScheduledActionRegistry | None = None,
    ) -> None:
        self.store = store
        self.triggers = triggers
        self.schedules = schedules or ScheduleDriverRegistry(
            [CronScheduleDriver()]
        )
        self.actions = actions or ScheduledActionRegistry(
            [PollTriggerBindingActionDriver(triggers)]
        )
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._started = False

    async def start(self) -> dict[str, int]:
        if self._started:
            return {"scheduled_task_runs": 0, "scheduled_tasks": 0}
        recovered = self.store.recover_scheduled_task_runs()
        self._scheduler.start(paused=True)
        self._started = True
        restored = 0
        for record in self.store.list_scheduled_tasks(enabled=True):
            try:
                self._install_job(record)
            except Exception as exc:
                self.store.set_scheduled_task_runtime_error(
                    record["id"], str(exc)
                )
            else:
                restored += 1
        self._scheduler.resume()
        return {
            "scheduled_task_runs": recovered,
            "scheduled_tasks": restored,
        }

    async def close(self) -> None:
        if not self._started:
            return
        self._started = False
        self._scheduler.shutdown(wait=False)
        current = asyncio.current_task()
        active = [
            task
            for task in self._active_tasks
            if task is not current and not task.done()
        ]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def create_task(
        self,
        definition: ScheduledTaskDefinition,
    ) -> dict[str, Any]:
        definition = self._normalize_definition(definition)
        record = self.store.create_scheduled_task(definition)
        if self._started and record["enabled"]:
            try:
                self._install_job(record)
            except Exception as exc:
                self.store.set_scheduled_task_runtime_error(
                    record["id"], str(exc)
                )
                raise
        return self.store.get_scheduled_task(record["id"])

    def update_task(
        self,
        task_id: str,
        definition: ScheduledTaskDefinition,
    ) -> dict[str, Any]:
        definition = self._normalize_definition(definition)
        record = self.store.update_scheduled_task(task_id, definition)
        self._remove_job(task_id)
        if self._started and record["enabled"]:
            try:
                self._install_job(record)
            except Exception as exc:
                self.store.set_scheduled_task_runtime_error(task_id, str(exc))
                raise
        return self.store.get_scheduled_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.store.get_scheduled_task(task_id)

    def list_tasks(
        self,
        *,
        include_archived: bool = False,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_scheduled_tasks(
            include_archived=include_archived,
            enabled=enabled,
        )

    def set_task_enabled(
        self,
        task_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        if enabled:
            record = self.store.get_scheduled_task(task_id)
            self._normalize_definition(self._definition_from_record(record))
        record = self.store.set_scheduled_task_enabled(task_id, enabled)
        self._remove_job(task_id)
        if self._started and enabled:
            try:
                self._install_job(record)
            except Exception as exc:
                self.store.set_scheduled_task_runtime_error(task_id, str(exc))
        return self.store.get_scheduled_task(task_id)

    def archive_task(self, task_id: str) -> dict[str, Any]:
        self._remove_job(task_id)
        return self.store.archive_scheduled_task(task_id)

    def refresh_tasks_for_binding(self, binding_id: str) -> None:
        for record in self.store.list_scheduled_tasks(enabled=True):
            if (
                record["action_type"] != "poll_trigger_binding"
                or record["action"].get("binding_id") != binding_id
            ):
                continue
            self._remove_job(record["id"])
            if not self._started:
                continue
            try:
                self._install_job(record)
            except Exception as exc:
                self.store.set_scheduled_task_runtime_error(
                    record["id"], str(exc)
                )

    async def run_now(self, task_id: str) -> dict[str, Any]:
        self.store.get_scheduled_task(task_id)
        return await self._execute_task(
            task_id,
            scheduled_for=utc_now().isoformat(),
        )

    def list_runs(
        self,
        task_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.store.list_scheduled_task_runs(task_id, limit=limit)

    def describe_schedule_types(self) -> list[dict[str, Any]]:
        return self.schedules.describe()

    def describe_action_types(self) -> list[dict[str, Any]]:
        return self.actions.describe()

    def _normalize_definition(
        self,
        definition: ScheduledTaskDefinition,
    ) -> ScheduledTaskDefinition:
        schedule_driver = self.schedules.get(definition.schedule_type)
        schedule = schedule_driver.validate(definition.schedule)
        schedule_driver.prepare(schedule)
        action = self.actions.get(definition.action_type).validate(
            definition.action
        )
        return definition.model_copy(
            update={"schedule": schedule, "action": action}
        )

    def _install_job(self, record: dict[str, Any]) -> None:
        definition = self._normalize_definition(
            self._definition_from_record(record)
        )
        prepared = self.schedules.get(definition.schedule_type).prepare(
            definition.schedule
        )
        job = self._scheduler.add_job(
            self._execute_scheduled_task,
            trigger=prepared.trigger,
            args=[definition.id],
            id=self._job_id(definition.id),
            replace_existing=True,
            coalesce=prepared.coalesce,
            misfire_grace_time=prepared.misfire_grace_seconds,
            max_instances=1,
        )
        self.store.set_scheduled_task_next_run(
            definition.id,
            self._datetime_text(job.next_run_time),
        )

    def _remove_job(self, task_id: str) -> None:
        if self._started:
            try:
                self._scheduler.remove_job(self._job_id(task_id))
            except JobLookupError:
                pass
        self.store.set_scheduled_task_next_run(task_id, None)

    async def _execute_scheduled_task(self, task_id: str) -> None:
        await self._execute_task(task_id, scheduled_for=utc_now().isoformat())

    async def _execute_task(
        self,
        task_id: str,
        *,
        scheduled_for: str,
    ) -> dict[str, Any]:
        current = asyncio.current_task()
        if current is not None:
            self._active_tasks.add(current)
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        try:
            async with lock:
                record = self.store.get_scheduled_task(task_id)
                definition = self._definition_from_record(record)
                run = self.store.start_scheduled_task_run(
                    task_id,
                    scheduled_for=scheduled_for,
                )
                try:
                    result = await self.actions.get(
                        definition.action_type
                    ).execute(definition.action)
                except asyncio.CancelledError:
                    self.store.finish_scheduled_task_run(
                        run["id"],
                        ScheduledTaskRunStatus.interrupted,
                        error="scheduled task was cancelled during shutdown",
                    )
                    raise
                except Exception as exc:
                    return self.store.finish_scheduled_task_run(
                        run["id"],
                        ScheduledTaskRunStatus.failed,
                        error=str(exc),
                    )
                return self.store.finish_scheduled_task_run(
                    run["id"],
                    ScheduledTaskRunStatus.succeeded,
                    result=result,
                )
        finally:
            self._sync_next_run(task_id)
            if current is not None:
                self._active_tasks.discard(current)

    def _sync_next_run(self, task_id: str) -> None:
        if not self._started:
            return
        job = self._scheduler.get_job(self._job_id(task_id))
        self.store.set_scheduled_task_next_run(
            task_id,
            None if job is None else self._datetime_text(job.next_run_time),
        )

    @staticmethod
    def _definition_from_record(
        record: dict[str, Any],
    ) -> ScheduledTaskDefinition:
        return ScheduledTaskDefinition.model_validate(
            {
                "id": record["id"],
                "version": record["version"],
                "name": record["name"],
                "schedule_type": record["schedule_type"],
                "schedule": record["schedule"],
                "action_type": record["action_type"],
                "action": record["action"],
                "enabled": record["enabled"],
            }
        )

    @classmethod
    def _job_id(cls, task_id: str) -> str:
        return cls._JOB_PREFIX + task_id

    @staticmethod
    def _datetime_text(value: Any | None) -> str | None:
        return None if value is None else value.isoformat()
