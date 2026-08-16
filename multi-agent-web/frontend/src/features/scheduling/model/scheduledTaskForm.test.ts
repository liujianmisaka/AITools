import { describe, expect, it } from "vitest";
import {
  newScheduledTaskValues,
  scheduledTaskValuesToDefinition,
} from "./scheduledTaskForm";

describe("scheduled task form contract", () => {
  it("builds the registered cron and trigger polling definition", () => {
    const values = newScheduledTaskValues();
    values.id = "poll-main";
    values.name = "Poll main";
    values.binding_id = "watch-main";
    values.expression = " */10   * * * * ";

    expect(scheduledTaskValuesToDefinition(values)).toEqual({
      id: "poll-main",
      version: 1,
      name: "Poll main",
      schedule_type: "cron",
      schedule: {
        expression: "*/10 * * * *",
        timezone: "Asia/Shanghai",
        misfire_grace_seconds: 60,
        coalesce: true,
      },
      action_type: "poll_trigger_binding",
      action: { binding_id: "watch-main" },
      enabled: true,
    });
  });

  it("rejects non five-field cron expressions", () => {
    const values = newScheduledTaskValues();
    values.expression = "0 0 * *";
    expect(() => scheduledTaskValuesToDefinition(values)).toThrow("五个字段");
  });

  it("builds interval and publish event definitions", () => {
    const values = newScheduledTaskValues();
    values.id = "tick-every-minute";
    values.name = "Tick every minute";
    values.schedule_type = "interval";
    values.action_type = "publish_trigger_event";
    values.seconds = 60;
    values.minutes = 0;
    values.timezone = "UTC";
    values.binding_id = "";

    expect(scheduledTaskValuesToDefinition(values)).toEqual({
      id: "tick-every-minute",
      version: 1,
      name: "Tick every minute",
      schedule_type: "interval",
      schedule: {
        weeks: 0,
        days: 0,
        hours: 0,
        minutes: 0,
        seconds: 60,
        start_at: null,
        end_at: null,
        timezone: "UTC",
        misfire_grace_seconds: 60,
        coalesce: true,
      },
      action_type: "publish_trigger_event",
      action: {},
      enabled: true,
    });
  });

  it("builds one-time definitions and rejects a missing run_at", () => {
    const values = newScheduledTaskValues();
    values.id = "once";
    values.name = "Once";
    values.schedule_type = "one_time";
    values.action_type = "publish_trigger_event";
    values.run_at = "2026-08-20T00:00:00Z";

    expect(scheduledTaskValuesToDefinition(values).schedule).toEqual({
      run_at: "2026-08-20T00:00:00Z",
      misfire_grace_seconds: 60,
    });
    values.run_at = "";
    expect(() => scheduledTaskValuesToDefinition(values)).toThrow("run_at");
  });
});
