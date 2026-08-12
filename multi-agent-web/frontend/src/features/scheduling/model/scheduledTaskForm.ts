import type {
  ScheduledTaskDefinition,
  ScheduledTaskRecord,
} from "../../../shared/types";

export interface ScheduledTaskEditorValues {
  id: string;
  version: number;
  name: string;
  schedule_type: string;
  expression: string;
  timezone: string;
  misfire_grace_seconds: number;
  coalesce: boolean;
  action_type: string;
  binding_id: string;
  enabled: boolean;
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
    expression: "*/5 * * * *",
    timezone: "Asia/Shanghai",
    misfire_grace_seconds: 60,
    coalesce: true,
    action_type: "poll_trigger_binding",
    binding_id: "",
    enabled: true,
  };
}

export function scheduledTaskRecordToValues(
  record: ScheduledTaskRecord,
): ScheduledTaskEditorValues {
  return {
    id: record.id,
    version: record.version,
    name: record.name,
    schedule_type: record.schedule_type,
    expression: String(record.schedule.expression ?? ""),
    timezone: String(record.schedule.timezone ?? "Asia/Shanghai"),
    misfire_grace_seconds: Number(
      record.schedule.misfire_grace_seconds ?? 60,
    ),
    coalesce: Boolean(record.schedule.coalesce ?? true),
    action_type: record.action_type,
    binding_id: String(record.action.binding_id ?? ""),
    enabled: record.enabled,
  };
}

export function scheduledTaskValuesToDefinition(
  values: ScheduledTaskEditorValues,
): ScheduledTaskDefinition {
  const expression = values.expression.trim().replace(/\s+/g, " ");
  if (expression.split(" ").length !== 5) {
    throw new Error("Cron 表达式必须包含五个字段");
  }
  return {
    id: values.id.trim(),
    version: values.version,
    name: values.name.trim(),
    schedule_type: values.schedule_type,
    schedule: {
      expression,
      timezone: values.timezone,
      misfire_grace_seconds: values.misfire_grace_seconds,
      coalesce: values.coalesce,
    },
    action_type: values.action_type,
    action: { binding_id: values.binding_id },
    enabled: values.enabled,
  };
}
