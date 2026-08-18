from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from misaka_kernel_contracts import JsonObject

from misaka_event_source.contracts import CloudEvent
from misaka_event_source.memory import MemoryEventSource


@dataclass(frozen=True, slots=True)
class TimerSchedule:
    interval_seconds: float
    source: str
    event_type: str
    max_occurrences: int | None = None
    start_immediately: bool = False

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if not self.source.strip() or not self.event_type.strip():
            raise ValueError("source and event_type must not be empty")
        if self.max_occurrences is not None and self.max_occurrences < 1:
            raise ValueError("max_occurrences must be positive when provided")


class CronSchedule:
    def __init__(self, expression: str, *, timezone: str = "UTC") -> None:
        if not expression.strip():
            raise ValueError("cron expression must not be empty")
        if not timezone.strip():
            raise ValueError("timezone must not be empty")
        try:
            self._zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("invalid timezone") from exc
        try:
            croniter(expression, datetime.now(UTC).astimezone(self._zone))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid cron expression") from exc
        self.expression = expression
        self.timezone = timezone

    def next_after(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        return croniter(self.expression, moment.astimezone(self._zone)).get_next(datetime)


class TimerEventSource:
    def __init__(
        self,
        schedule: TimerSchedule,
        payload_factory: Callable[[int, datetime], JsonObject | Awaitable[JsonObject]],
    ) -> None:
        self.schedule = schedule
        self._payload_factory = payload_factory
        self._source = MemoryEventSource()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._errors: list[str] = []

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(self._errors)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def wait(self) -> None:
        task = self._task
        if task is None:
            raise RuntimeError("timer source has not been started")
        await task

    async def events(self, *, start_sequence: int = 1):
        async for event in self._source.events(start_sequence=start_sequence):
            yield event

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._source.close()

    async def _run(self) -> None:
        count = 0
        if not self.schedule.start_immediately:
            await asyncio.sleep(self.schedule.interval_seconds)
        while not self._closed:
            count += 1
            now = datetime.now(UTC)
            try:
                payload = self._payload_factory(count, now)
                if isawaitable(payload):
                    payload = await payload
                await self._source.publish(
                    CloudEvent(
                        event_id=f"timer:{self.schedule.source}:{count}",
                        source=self.schedule.source,
                        event_type=self.schedule.event_type,
                        data=payload,
                        occurred_at=now,
                    )
                )
            except Exception as exc:
                self._errors.append(str(exc))
                return
            if self.schedule.max_occurrences is not None and count >= self.schedule.max_occurrences:
                return
            await asyncio.sleep(self.schedule.interval_seconds)
