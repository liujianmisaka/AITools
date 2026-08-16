from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import cast

from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleCalendarSpec,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
)

from multi_agent_v2.packages.control_plane.models import (
    GitRefTarget,
    ScheduleRecord,
    ScheduleTriggerInput,
)
from multi_agent_v2.packages.domain.json_types import JsonObject, JsonValue
from multi_agent_v2.packages.workflow_runtime.workflow import ORCHESTRATION_TASK_QUEUE


class ScheduleContractError(ValueError):
    """A persisted schedule cannot be represented by the supported Temporal contract."""


def build_temporal_schedule(record: ScheduleRecord) -> Schedule:
    action = _action(record)
    spec = _spec(record)
    return Schedule(
        action=action,
        spec=spec,
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=timedelta(minutes=5),
            pause_on_failure=True,
        ),
        state=ScheduleState(
            paused=not record.enabled,
            note=f"managed by multi-agent-v2 revision {record.revision}",
        ),
    )


def validate_schedule_record(record: ScheduleRecord) -> None:
    build_temporal_schedule(record)


def _action(record: ScheduleRecord) -> ScheduleActionStartWorkflow:
    if record.target_kind == "workflow":
        _workflow_target(record.target)
        trigger = ScheduleTriggerInput(
            schedule_id=record.schedule_id,
            schedule_revision=record.revision,
            target=record.target,
        )
        return ScheduleActionStartWorkflow(
            "ScheduleTriggerWorkflow",
            trigger,
            id=f"multi-agent-v2/schedule-trigger/{record.schedule_id}",
            task_queue=ORCHESTRATION_TASK_QUEUE,
            static_summary=f"schedule:{record.schedule_id}",
        )
    target = GitRefTarget.model_validate(record.target)
    return ScheduleActionStartWorkflow(
        "GitConnectorWorkflow",
        target,
        id=f"multi-agent-v2/git-connector/{record.schedule_id}",
        task_queue=ORCHESTRATION_TASK_QUEUE,
        static_summary=f"git-schedule:{record.schedule_id}",
    )


def _spec(record: ScheduleRecord) -> ScheduleSpec:
    raw = record.schedule_spec
    timezone = _optional_text(raw.get("timeZone"), "schedule timeZone")
    if record.schedule_kind == "cron":
        expressions = raw.get("expressions")
        if (
            not isinstance(expressions, list)
            or not expressions
            or any(not isinstance(item, str) or not item.strip() for item in expressions)
        ):
            raise ScheduleContractError("cron schedule requires non-empty expressions")
        return ScheduleSpec(
            cron_expressions=tuple(cast(Sequence[str], expressions)),
            time_zone_name=timezone,
        )
    if record.schedule_kind == "interval":
        every_seconds = _positive_number(raw.get("everySeconds"), "interval everySeconds")
        offset_value = raw.get("offsetSeconds")
        offset_seconds = (
            _nonnegative_number(offset_value, "interval offsetSeconds")
            if offset_value is not None
            else None
        )
        if offset_seconds is not None and offset_seconds >= every_seconds:
            raise ScheduleContractError("interval offsetSeconds must be lower than everySeconds")
        return ScheduleSpec(
            intervals=(
                ScheduleIntervalSpec(
                    every=timedelta(seconds=every_seconds),
                    offset=(
                        timedelta(seconds=offset_seconds) if offset_seconds is not None else None
                    ),
                ),
            ),
            time_zone_name=timezone,
        )
    calendar = ScheduleCalendarSpec(
        second=_ranges(raw.get("second"), "second", default=(0,)),
        minute=_ranges(raw.get("minute"), "minute", default=(0,)),
        hour=_ranges(raw.get("hour"), "hour", default=(0,)),
        day_of_month=_ranges(raw.get("dayOfMonth"), "dayOfMonth", default=(1, 31)),
        month=_ranges(raw.get("month"), "month", default=(1, 12)),
        year=_ranges(raw.get("year"), "year", default=()),
        day_of_week=_ranges(raw.get("dayOfWeek"), "dayOfWeek", default=(0, 6)),
    )
    return ScheduleSpec(calendars=(calendar,), time_zone_name=timezone)


def _ranges(
    raw: JsonValue,
    name: str,
    *,
    default: tuple[int, ...],
) -> tuple[ScheduleRange, ...]:
    if raw is None:
        if len(default) == 2:
            return (ScheduleRange(start=default[0], end=default[1]),)
        return tuple(ScheduleRange(start=value) for value in default)
    if not isinstance(raw, list):
        raise ScheduleContractError(f"calendar {name} must be an array")
    ranges: list[ScheduleRange] = []
    for item in raw:
        if isinstance(item, bool):
            raise ScheduleContractError(f"calendar {name} contains an invalid boolean")
        if isinstance(item, int):
            ranges.append(ScheduleRange(start=item))
            continue
        if not isinstance(item, dict):
            raise ScheduleContractError(f"calendar {name} contains an invalid range")
        start = item.get("start")
        end = item.get("end", start)
        step = item.get("step", 1)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in (start, end, step)
        ):
            raise ScheduleContractError(f"calendar {name} range must contain integers")
        assert isinstance(start, int) and isinstance(end, int) and isinstance(step, int)
        if step < 1 or end < start:
            raise ScheduleContractError(f"calendar {name} range is invalid")
        ranges.append(ScheduleRange(start=start, end=end, step=step))
    return tuple(ranges)


def _workflow_target(target: JsonObject) -> None:
    template_id = target.get("templateId")
    template_version = target.get("templateVersion")
    workflow_input = target.get("workflowInput", {})
    if (
        not isinstance(template_id, str)
        or not template_id
        or isinstance(template_version, bool)
        or not isinstance(template_version, int)
        or template_version < 1
        or not isinstance(workflow_input, dict)
    ):
        raise ScheduleContractError("workflow schedule target is invalid")


def _positive_number(value: JsonValue, label: str) -> float:
    number = _nonnegative_number(value, label)
    if number <= 0:
        raise ScheduleContractError(f"{label} must be positive")
    return number


def _nonnegative_number(value: JsonValue, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ScheduleContractError(f"{label} must be a non-negative number")
    return float(value)


def _optional_text(value: JsonValue, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ScheduleContractError(f"{label} must be a non-empty string")
    return value.strip()
