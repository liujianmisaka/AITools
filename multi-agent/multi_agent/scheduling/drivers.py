from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ValidationError

from multi_agent.domain.errors import ScheduleConfigurationError
from multi_agent.domain.models import (
    CronScheduleConfig,
    PollTriggerBindingActionConfig,
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
