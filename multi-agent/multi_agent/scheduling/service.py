from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from multi_agent.domain.errors import ScheduledTaskNotFoundError
from multi_agent.domain.models import (
    ScheduledTaskDefinition,
    ScheduledTaskRunStatus,
    TriggerEventInput,
    utc_now,
)
from multi_agent.scheduling.drivers import (
    CronScheduleDriver,
    IntervalScheduleDriver,
    OneTimeScheduleDriver,
    PollTriggerBindingActionDriver,
    PublishTriggerEventActionDriver,
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
            [
                CronScheduleDriver(),
                IntervalScheduleDriver(),
                OneTimeScheduleDriver(),
            ]
        )
        self.actions = actions or ScheduledActionRegistry(
            [
                PollTriggerBindingActionDriver(triggers),
                PublishTriggerEventActionDriver(triggers),
            ]
        )
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._missed_one_time_handled: set[str] = set()
        self._background_errors: deque[str] = deque(maxlen=50)
        self._unrecovered_background_failures: dict[str, str] = {}
        self._started = False

    async def start(self) -> dict[str, int]:
        if self._started:
            return {"scheduled_task_runs": 0, "scheduled_tasks": 0}
        recovered = self.store.recover_scheduled_task_runs()
        self._scheduler.add_listener(
            self._on_scheduler_event,
            EVENT_JOB_MISSED,
        )
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
                self._mark_current_fault(record["id"], str(exc))
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
                self._mark_current_fault(record["id"], str(exc))
                raise
        else:
            self._clear_current_fault(record["id"])
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
                self._mark_current_fault(task_id, str(exc))
                raise
        else:
            self._clear_current_fault(task_id)
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
                self._mark_current_fault(task_id, str(exc))
        else:
            self._clear_current_fault(task_id)
        return self.store.get_scheduled_task(task_id)

    def archive_task(self, task_id: str) -> dict[str, Any]:
        self._remove_job(task_id)
        record = self.store.archive_scheduled_task(task_id)
        self._clear_current_fault(task_id)
        return record

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
                self._mark_current_fault(record["id"], str(exc))

    async def run_now(self, task_id: str) -> dict[str, Any]:
        self.store.get_scheduled_task(task_id)
        return await self._execute_task(
            task_id,
            scheduled_for=utc_now().isoformat(),
            scheduled_fire=False,
        )

    def list_runs(
        self,
        task_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.store.list_scheduled_task_runs(task_id, limit=limit)

    def background_errors(self) -> list[str]:
        return list(self._background_errors)

    def current_background_failures(self) -> dict[str, str]:
        return dict(self._unrecovered_background_failures)

    def _mark_current_fault(self, task_id: str, error: str) -> None:
        self._unrecovered_background_failures[task_id] = error
        self._background_errors.append(error)

    def _clear_current_fault(self, task_id: str) -> None:
        self._unrecovered_background_failures.pop(task_id, None)

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
        next_run_text = self._datetime_text(job.next_run_time)
        self.store.set_scheduled_task_next_run(
            definition.id,
            next_run_text,
        )
        self._missed_one_time_handled.discard(definition.id)
        self._clear_current_fault(definition.id)
        self._record_expired_one_time_if_needed(
            definition.id,
            definition.schedule_type,
            definition.schedule.get("misfire_grace_seconds", 0),
            next_run_text,
        )

    def _remove_job(self, task_id: str) -> None:
        if self._started:
            try:
                self._scheduler.remove_job(self._job_id(task_id))
            except JobLookupError:
                pass
        self.store.set_scheduled_task_next_run(task_id, None)

    def _on_scheduler_event(self, event: Any) -> None:
        if not self._started or event.code != EVENT_JOB_MISSED:
            return
        job_id = str(event.job_id or "")
        if not job_id.startswith(self._JOB_PREFIX):
            return
        task_id = job_id[len(self._JOB_PREFIX):]
        scheduled_for = utc_now().isoformat()
        scheduled_time = getattr(event, "scheduled_run_time", None)
        if scheduled_time is not None:
            scheduled_for = self._datetime_text(scheduled_time) or scheduled_for
        self._spawn_missed_one_time_handler(task_id, scheduled_for)

    async def _handle_missed_one_time(
        self,
        task_id: str,
        scheduled_for: str,
    ) -> None:
        if task_id in self._missed_one_time_handled:
            return
        try:
            run = self._record_missed_one_time(
                task_id,
                scheduled_for,
                "scheduler missed the one-time run beyond the misfire grace",
            )
        except Exception:
            self._missed_one_time_handled.discard(task_id)
            raise
        if run is not None:
            self.triggers.notify_outbox()

    def _persist_missed_terminal_failure(
        self,
        task_id: str,
        scheduled_for: str,
        message: str,
    ) -> bool:
        terminal_error = f"missed handler failed permanently: {message}"
        run_id = "missed-terminal-" + hashlib.sha256(
            f"{task_id}\0{scheduled_for}\0terminal".encode("utf-8")
        ).hexdigest()
        event = self._schedule_run_internal_event(
            task_id,
            run_id,
            ScheduledTaskRunStatus.failed.value,
            scheduled_for,
            terminal_error,
        )
        try:
            self.store.record_failed_scheduled_task_run(
                task_id,
                run_id=run_id,
                scheduled_for=scheduled_for,
                error=terminal_error,
                internal_event=event,
            )
        except ScheduledTaskNotFoundError:
            # There is no task to disable or mark and no remediation action
            # for the user. Keep the bounded diagnostic history, but remove
            # the current-fault entry so health can return to ok.
            self._unrecovered_background_failures.pop(task_id, None)
            return True
        except Exception:
            return False
        return True

    def _record_expired_one_time_if_needed(
        self,
        task_id: str,
        schedule_type: str,
        misfire_grace_seconds: int,
        next_run_text: str | None,
    ) -> dict[str, Any] | None:
        if schedule_type != "one_time" or next_run_text is None:
            return None
        try:
            next_run = utc_now()
            parsed = datetime.fromisoformat(next_run_text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            next_run = parsed.astimezone(timezone.utc)
        except ValueError:
            return None
        if utc_now() <= next_run + timedelta(seconds=misfire_grace_seconds):
            return None
        run = self._record_missed_one_time(
            task_id,
            next_run_text,
            "one-time run_at is older than the misfire grace period",
        )
        if run is not None:
            self._remove_job(task_id)
            self.triggers.notify_outbox()
        return run

    def _spawn_tracked_task(
        self,
        coroutine: Any,
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        if not self._started:
            raise RuntimeError("scheduler is not running")
        task = asyncio.create_task(coroutine, name=name)
        self._active_tasks.add(task)
        task.add_done_callback(self._observe_background_task)
        return task

    def _observe_background_task(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except (asyncio.CancelledError, Exception):
            return
        if error is not None:
            self._background_errors.append(str(error))

    def _spawn_missed_one_time_handler(
        self,
        task_id: str,
        scheduled_for: str,
    ) -> None:
        async def supervised() -> None:
            attempt = 0
            last_handler_error: str | None = None
            terminal_persisted = False
            unavailable_recorded = False
            while self._started:
                try:
                    await self._handle_missed_one_time(
                        task_id, scheduled_for
                    )
                    self._unrecovered_background_failures.pop(
                        task_id, None
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    attempt = min(attempt + 1, 6)
                    message = str(exc)
                    self._unrecovered_background_failures[task_id] = message
                    if message != last_handler_error:
                        self._background_errors.append(message)
                        last_handler_error = message
                    if attempt >= 3 and not terminal_persisted:
                        terminal_persisted = self._persist_missed_terminal_failure(
                            task_id, scheduled_for, message
                        )
                        if terminal_persisted:
                            return
                        unavailable_message = (
                            "missed terminal failure could not be persisted; "
                            "storage is unavailable"
                        )
                        if not unavailable_recorded:
                            self._background_errors.append(
                                unavailable_message
                            )
                            unavailable_recorded = True
                    await asyncio.sleep(
                        min(5.0, 0.1 * (2 ** attempt))
                    )

        self._spawn_tracked_task(
            supervised(),
            name="multi-agent-missed-one-time-handler",
        )

    def _record_missed_one_time(
        self,
        task_id: str,
        scheduled_for: str,
        reason: str,
    ) -> dict[str, Any] | None:
        if task_id in self._missed_one_time_handled:
            return None
        record = self.store.get_scheduled_task(task_id)
        if not record["enabled"] or record["schedule_type"] != "one_time":
            return None
        self._missed_one_time_handled.add(task_id)
        run_id = "missed-" + hashlib.sha256(
            f"{task_id}\0{scheduled_for}\0missed".encode("utf-8")
        ).hexdigest()
        return self.store.record_failed_scheduled_task_run(
            task_id,
            run_id=run_id,
            scheduled_for=scheduled_for,
            error=reason,
            internal_event=self._schedule_run_internal_event(
                task_id,
                run_id,
                ScheduledTaskRunStatus.failed.value,
                scheduled_for,
                reason,
            ),
        )

    async def _execute_scheduled_task(self, task_id: str) -> None:
        record = self.store.get_scheduled_task(task_id)
        scheduled_for = record.get("next_run_at") or utc_now().isoformat()
        await self._execute_task(
            task_id,
            scheduled_for=scheduled_for,
            scheduled_fire=True,
        )

    async def _execute_task(
        self,
        task_id: str,
        *,
        scheduled_for: str,
        scheduled_fire: bool = False,
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
                    ).execute_with_context(
                        definition.action,
                        task_id=task_id,
                        run_id=run["id"],
                        scheduled_for=scheduled_for,
                        schedule_type=definition.schedule_type,
                    )
                except asyncio.CancelledError:
                    interrupted_error = (
                        "scheduled task was cancelled during shutdown"
                    )
                    interrupted = self.store.finish_scheduled_task_run(
                        run["id"],
                        ScheduledTaskRunStatus.interrupted,
                        error=interrupted_error,
                        internal_event=self._schedule_run_internal_event(
                            task_id,
                            run["id"],
                            ScheduledTaskRunStatus.interrupted.value,
                            scheduled_for,
                            interrupted_error,
                        ),
                    )
                    self._complete_one_time_task(
                        task_id,
                        definition.schedule_type,
                        scheduled_fire,
                    )
                    await self._publish_schedule_run_updated(interrupted)
                    raise
                except Exception as exc:
                    failed_error = str(exc)
                    failed = self.store.finish_scheduled_task_run(
                        run["id"],
                        ScheduledTaskRunStatus.failed,
                        error=failed_error,
                        internal_event=self._schedule_run_internal_event(
                            task_id,
                            run["id"],
                            ScheduledTaskRunStatus.failed.value,
                            scheduled_for,
                            failed_error,
                        ),
                    )
                    self._complete_one_time_task(
                        task_id,
                        definition.schedule_type,
                        scheduled_fire,
                    )
                    await self._publish_schedule_run_updated(failed)
                    return failed
                finished = self.store.finish_scheduled_task_run(
                    run["id"],
                    ScheduledTaskRunStatus.succeeded,
                    result=result,
                    internal_event=self._schedule_run_internal_event(
                        task_id,
                        run["id"],
                        ScheduledTaskRunStatus.succeeded.value,
                        scheduled_for,
                        None,
                    ),
                )
                self._complete_one_time_task(
                    task_id,
                    definition.schedule_type,
                    scheduled_fire,
                )
                await self._publish_schedule_run_updated(finished)
                return finished
        finally:
            self._sync_next_run(task_id)
            if current is not None:
                self._active_tasks.discard(current)

    def _complete_one_time_task(
        self,
        task_id: str,
        schedule_type: str,
        scheduled_fire: bool,
    ) -> None:
        if not scheduled_fire or schedule_type != "one_time":
            return
        try:
            record = self.store.get_scheduled_task(task_id)
            if record["enabled"]:
                self.store.set_scheduled_task_enabled(task_id, False)
        except Exception:
            # The run result is already durable; a concurrent archive/update
            # must not be reported as a failed scheduled action.
            return

    @staticmethod
    def _schedule_run_internal_event(
        task_id: str,
        run_id: str,
        status: str,
        scheduled_for: str,
        error: str | None,
    ) -> TriggerEventInput:
        return TriggerEventInput(
            source_type="internal",
            event_type="schedule.run.updated",
            event_version=1,
            source_key=task_id,
            dedup_key=f"schedule-run-updated:{task_id}:{run_id}:{status}",
            payload={
                "scheduled_task_id": task_id,
                "run_id": run_id,
                "status": status,
                "scheduled_for": scheduled_for,
                "error": error,
            },
        )

    async def _publish_schedule_run_updated(
        self,
        run: dict[str, Any],
    ) -> None:
        try:
            await self.triggers.publish_internal(
                TriggerEventInput(
                    source_type="internal",
                    event_type="schedule.run.updated",
                    event_version=1,
                    source_key=run["scheduled_task_id"],
                    dedup_key=(
                        f"schedule-run-updated:{run['scheduled_task_id']}:"
                        f"{run['id']}:{run['status']}"
                    ),
                    payload={
                        "scheduled_task_id": run["scheduled_task_id"],
                        "run_id": run["id"],
                        "status": run["status"],
                        "scheduled_for": run["scheduled_for"],
                        "error": run["error"],
                    },
                )
            )
        except Exception:
            # The run outcome is already durable. A failed internal
            # notification must not change the scheduled task result.
            return

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
