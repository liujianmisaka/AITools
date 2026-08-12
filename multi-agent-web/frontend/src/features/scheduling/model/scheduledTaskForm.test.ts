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
});
