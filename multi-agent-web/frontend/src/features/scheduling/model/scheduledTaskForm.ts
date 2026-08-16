import type {
  ScheduledTaskDefinition,
  ScheduledTaskRecord,
} from "../../../shared/types";

export interface ScheduledTaskEditorValues {
  id: string;
  version: number;
  name: string;
  schedule_type: string;
  action_type: string;
  enabled: boolean;
  expression: string;
  timezone: string;
  misfire_grace_seconds: number;
  coalesce: boolean;
  seconds: number;
  minutes: number;
  hours: number;
  days: number;
  weeks: number;
  start_at: string;
  end_at: string;
  run_at: string;
  binding_id: string;
}

export function createScheduledTaskId(): string {
  return `schedule_${crypto.randomUUID().replaceAll("-", "").slice(0, 20)}`;
}

export function newScheduledTaskValues(): ScheduledTaskEditorValues {
  return {
    id: createScheduledTaskId(),
    version: 1,
    name: "",
    schedule_type: "cron",
    action_type: "poll_trigger_binding",
    enabled: true,
    expression: "*/5 * * * *",
    timezone: "Asia/Shanghai",
    misfire_grace_seconds: 60,
    coalesce: true,
    seconds: 60,
    minutes: 0,
    hours: 0,
    days: 0,
    weeks: 0,
    start_at: "",
    end_at: "",
    run_at: "",
    binding_id: "",
  };
}

function nullableIso(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function numberValue(value: unknown, fallback: number): number {
  return Number(value ?? fallback);
}

export function scheduledTaskRecordToValues(
  record: ScheduledTaskRecord,
): ScheduledTaskEditorValues {
  const schedule = record.schedule;
  return {
    id: record.id,
    version: record.version,
    name: record.name,
    schedule_type: record.schedule_type,
    action_type: record.action_type,
    enabled: record.enabled,
    expression: String(schedule.expression ?? ""),
    timezone: String(schedule.timezone ?? "Asia/Shanghai"),
    misfire_grace_seconds: numberValue(schedule.misfire_grace_seconds, 60),
    coalesce: Boolean(schedule.coalesce ?? true),
    seconds: numberValue(schedule.seconds, 0),
    minutes: numberValue(schedule.minutes, 0),
    hours: numberValue(schedule.hours, 0),
    days: numberValue(schedule.days, 0),
    weeks: numberValue(schedule.weeks, 0),
    start_at: String(schedule.start_at ?? ""),
    end_at: String(schedule.end_at ?? ""),
    run_at: String(schedule.run_at ?? ""),
    binding_id: String(record.action.binding_id ?? ""),
  };
}

function cronSchedule(values: ScheduledTaskEditorValues): Record<string, unknown> {
  const expression = values.expression.trim().replace(/\s+/g, " ");
  if (expression.split(" ").length !== 5) {
    throw new Error("Cron 表达式必须包含五个字段");
  }
  return {
    expression,
    timezone: values.timezone,
    misfire_grace_seconds: values.misfire_grace_seconds,
    coalesce: values.coalesce,
  };
}

function intervalSchedule(values: ScheduledTaskEditorValues): Record<string, unknown> {
  if (![values.seconds, values.minutes, values.hours, values.days, values.weeks]
    .some((value) => value > 0)) {
    throw new Error("Interval 计划至少需要一个非零时间字段");
  }
  if (values.start_at && values.end_at && values.start_at >= values.end_at) {
    throw new Error("Interval 开始时间必须早于结束时间");
  }
  return {
    weeks: values.weeks,
    days: values.days,
    hours: values.hours,
    minutes: values.minutes,
    seconds: values.seconds,
    start_at: nullableIso(values.start_at),
    end_at: nullableIso(values.end_at),
    timezone: values.timezone,
    misfire_grace_seconds: values.misfire_grace_seconds,
    coalesce: values.coalesce,
  };
}

function oneTimeSchedule(values: ScheduledTaskEditorValues): Record<string, unknown> {
  const runAt = nullableIso(values.run_at);
  if (!runAt) {
    throw new Error("One-time 计划必须填写 run_at");
  }
  return {
    run_at: runAt,
    misfire_grace_seconds: values.misfire_grace_seconds,
  };
}

export function scheduledTaskValuesToDefinition(
  values: ScheduledTaskEditorValues,
): ScheduledTaskDefinition {
  let schedule: Record<string, unknown>;
  if (values.schedule_type === "cron") {
    schedule = cronSchedule(values);
  } else if (values.schedule_type === "interval") {
    schedule = intervalSchedule(values);
  } else if (values.schedule_type === "one_time") {
    schedule = oneTimeSchedule(values);
  } else {
    throw new Error(`未知计划类型：${values.schedule_type}`);
  }
  const id = values.id.trim();
  const name = values.name.trim();
  if (!id || !name) {
    throw new Error("任务 ID 和名称不能为空");
  }
  const action: Record<string, unknown> =
    values.action_type === "poll_trigger_binding"
      ? { binding_id: values.binding_id.trim() }
      : {};
  return {
    id,
    version: values.version,
    name,
    schedule_type: values.schedule_type,
    schedule,
    action_type: values.action_type,
    action,
    enabled: values.enabled,
  };
}
