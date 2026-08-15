from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel, ValidationError

from multi_agent.domain.errors import ScheduleConfigurationError
from multi_agent.domain.models import (
    CronScheduleConfig,
    IntervalScheduleConfig,
    OneTimeScheduleConfig,
    PollTriggerBindingActionConfig,
    PublishTriggerEventActionConfig,
    TriggerEventInput,
)
from multi_agent.triggers.service import TriggerService


@dataclass(frozen=True, slots=True)
class PreparedSchedule:
    trigger: BaseTrigger
    coalesce: bool
    misfire_grace_seconds: int


class ScheduleDriver(ABC):
    schedule_type: str
    config_model: type[BaseModel]

    def describe(self) -> dict[str, Any]:
        return {
            "schedule_type": self.schedule_type,
            "config_schema": self.config_model.model_json_schema(),
        }

    def validate(self, config: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = self.config_model.model_validate(config)
        except ValidationError as exc:
            raise ScheduleConfigurationError(
                f"invalid {self.schedule_type!r} schedule config: {exc}"
            ) from exc
        return parsed.model_dump(mode="json")

    @abstractmethod
    def prepare(self, config: Mapping[str, Any]) -> PreparedSchedule:
        raise NotImplementedError


class CronScheduleDriver(ScheduleDriver):
    schedule_type = "cron"
    config_model = CronScheduleConfig

    def prepare(self, config: Mapping[str, Any]) -> PreparedSchedule:
        parsed = CronScheduleConfig.model_validate(self.validate(config))
        try:
            timezone = ZoneInfo(parsed.timezone)
            trigger = CronTrigger.from_crontab(
                parsed.expression,
                timezone=timezone,
            )
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ScheduleConfigurationError(
                "invalid cron schedule "
                f"{parsed.expression!r} in timezone {parsed.timezone!r}: {exc}"
            ) from exc
        return PreparedSchedule(
            trigger=trigger,
            coalesce=parsed.coalesce,
            misfire_grace_seconds=parsed.misfire_grace_seconds,
        )


class IntervalScheduleDriver(ScheduleDriver):
    schedule_type = "interval"
    config_model = IntervalScheduleConfig

    def prepare(self, config: Mapping[str, Any]) -> PreparedSchedule:
        parsed = IntervalScheduleConfig.model_validate(self.validate(config))
        try:
            timezone = ZoneInfo(parsed.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ScheduleConfigurationError(
                f"invalid interval timezone {parsed.timezone!r}: {exc}"
            ) from exc
        start_date = self._as_aware(parsed.start_at, timezone)
        end_date = self._as_aware(parsed.end_at, timezone)
        trigger = IntervalTrigger(
            weeks=parsed.weeks,
            days=parsed.days,
            hours=parsed.hours,
            minutes=parsed.minutes,
            seconds=parsed.seconds,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
        )
        return PreparedSchedule(
            trigger=trigger,
            coalesce=parsed.coalesce,
            misfire_grace_seconds=parsed.misfire_grace_seconds,
        )

    @staticmethod
    def _as_aware(value: datetime | None, timezone: ZoneInfo) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone)
        return value.astimezone(timezone)


class OneTimeScheduleDriver(ScheduleDriver):
    schedule_type = "one_time"
    config_model = OneTimeScheduleConfig

    def prepare(self, config: Mapping[str, Any]) -> PreparedSchedule:
        parsed = OneTimeScheduleConfig.model_validate(self.validate(config))
        run_at = parsed.run_at
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        run_at = run_at.astimezone(timezone.utc)
        return PreparedSchedule(
            trigger=DateTrigger(run_date=run_at, timezone=timezone.utc),
            coalesce=True,
            misfire_grace_seconds=parsed.misfire_grace_seconds,
        )


class ScheduledActionDriver(ABC):
    action_type: str
    config_model: type[BaseModel]

    def describe(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "config_schema": self.config_model.model_json_schema(),
        }

    def validate(self, config: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = self.config_model.model_validate(config)
        except ValidationError as exc:
            raise ScheduleConfigurationError(
                f"invalid {self.action_type!r} action config: {exc}"
            ) from exc
        self.validate_config(parsed)
        return parsed.model_dump(mode="json")

    def validate_config(self, config: BaseModel) -> None:
        del config

    @abstractmethod
    async def execute(self, config: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def execute_with_context(
        self,
        config: Mapping[str, Any],
        *,
        task_id: str,
        run_id: str,
        scheduled_for: str,
        schedule_type: str,
    ) -> dict[str, Any]:
        del task_id, run_id, scheduled_for, schedule_type
        return await self.execute(config)


class PollTriggerBindingActionDriver(ScheduledActionDriver):
    action_type = "poll_trigger_binding"
    config_model = PollTriggerBindingActionConfig

    def __init__(self, triggers: TriggerService) -> None:
        self._triggers = triggers

    def validate_config(self, config: BaseModel) -> None:
        parsed = PollTriggerBindingActionConfig.model_validate(config)
        self._triggers.validate_poll_binding(parsed.binding_id)

    async def execute(self, config: Mapping[str, Any]) -> dict[str, Any]:
        parsed = PollTriggerBindingActionConfig.model_validate(
            self.validate(config)
        )
        return await self._triggers.poll_binding(parsed.binding_id)


class PublishTriggerEventActionDriver(ScheduledActionDriver):
    action_type = "publish_trigger_event"
    config_model = PublishTriggerEventActionConfig

    def __init__(self, triggers: TriggerService) -> None:
        self._triggers = triggers

    async def execute(self, config: Mapping[str, Any]) -> dict[str, Any]:
        del config
        raise ScheduleConfigurationError(
            "publish_trigger_event requires scheduler run context"
        )

    async def execute_with_context(
        self,
        config: Mapping[str, Any],
        *,
        task_id: str,
        run_id: str,
        scheduled_for: str,
        schedule_type: str,
    ) -> dict[str, Any]:
        self.validate(config)
        sequence = self._triggers.store.count_scheduled_task_runs(task_id)
        event = TriggerEventInput(
            source_type="schedule",
            event_type="schedule.tick",
            event_version=1,
            source_key=task_id,
            dedup_key=f"schedule-tick:{task_id}:{run_id}",
            payload={
                "schedule_id": task_id,
                "schedule_type": schedule_type,
                "scheduled_fire_time": scheduled_for,
                "sequence": sequence,
            },
        )
        return await self._triggers.publish_internal(event)


class ScheduleDriverRegistry:
    def __init__(self, drivers: Iterable[ScheduleDriver] = ()) -> None:
        self._drivers: dict[str, ScheduleDriver] = {}
        for driver in drivers:
            self.register(driver)

    def register(self, driver: ScheduleDriver) -> None:
        if driver.schedule_type in self._drivers:
            raise ValueError(
                f"schedule driver already registered: {driver.schedule_type}"
            )
        self._drivers[driver.schedule_type] = driver

    def get(self, schedule_type: str) -> ScheduleDriver:
        try:
            return self._drivers[schedule_type]
        except KeyError as exc:
            raise ScheduleConfigurationError(
                f"schedule type is not registered: {schedule_type}"
            ) from exc

    def describe(self) -> list[dict[str, Any]]:
        return [
            driver.describe()
            for driver in sorted(
                self._drivers.values(),
                key=lambda item: item.schedule_type,
            )
        ]


class ScheduledActionRegistry:
    def __init__(self, drivers: Iterable[ScheduledActionDriver] = ()) -> None:
        self._drivers: dict[str, ScheduledActionDriver] = {}
        for driver in drivers:
            self.register(driver)

    def register(self, driver: ScheduledActionDriver) -> None:
        if driver.action_type in self._drivers:
            raise ValueError(
                f"scheduled action already registered: {driver.action_type}"
            )
        self._drivers[driver.action_type] = driver

    def get(self, action_type: str) -> ScheduledActionDriver:
        try:
            return self._drivers[action_type]
        except KeyError as exc:
            raise ScheduleConfigurationError(
                f"scheduled action is not registered: {action_type}"
            ) from exc

    def describe(self) -> list[dict[str, Any]]:
        return [
            driver.describe()
            for driver in sorted(
                self._drivers.values(),
                key=lambda item: item.action_type,
            )
        ]
