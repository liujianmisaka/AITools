import { describe, expect, it } from "vitest";
import {
  newTriggerValues,
  parseJsonObject,
  triggerValuesToDefinition,
} from "./triggerForm";

describe("trigger form contract", () => {
  it("builds the canonical Git source key and structured source config", () => {
    const values = newTriggerValues("git_commit");
    values.id = "watch-main";
    values.name = "Watch main";
    values.template_id = "flow";
    values.workspace_id = "aitools";
    values.remote = "origin";
    values.branch = "main";
    values.event_filter_text = '{"update_kind":"forward"}';
    values.input_mapping_text = '{"sha":"after_sha"}';

    expect(triggerValuesToDefinition(values)).toMatchObject({
      source_key: "aitools:origin:main",
      source_config: {
        workspace_id: "aitools",
        remote: "origin",
        branch: "main",
        fetch: true,
      },
      event_filter: { update_kind: "forward" },
      input_mapping: { sha: "after_sha" },
    });
  });

  it("rejects arrays and malformed JSON where an object is required", () => {
    expect(() => parseJsonObject("[]", "Payload")).toThrow("JSON 对象");
    expect(() => parseJsonObject("{", "Payload")).toThrow("有效的 JSON");
  });
});
