import { describe, expect, it } from "vitest";
import type { ProviderDescription } from "../types";
import {
  buildWorkflow,
  createTaskDraft,
  strictObjectSchemaTemplate,
  validateCodexOutputSchema,
} from "./workflow";

const providers: ProviderDescription[] = [
  {
    name: "codex",
    available: true,
    capabilities: { read_only_mode: true },
    models: [
      {
        id: "sensenova/deepseek-v4-flash",
        label: "DeepSeek V4 Flash",
        model_type: "sensenova",
        efforts: ["low", "high"],
      },
    ],
  },
];

describe("workflow contract", () => {
  it("requires an explicit configured model and reasoning level", () => {
    const task = createTaskDraft(providers, ["aitools"]);
    task.prompt_template = "Read files";
    expect(() => buildWorkflow("demo", [task], providers, 1, "fail_fast")).toThrow(
      "必须选择模型类型",
    );
  });

  it("builds provider options without falling back to defaults", () => {
    const task = createTaskDraft(providers, ["aitools"]);
    task.prompt_template = "Read files";
    task.model_type = "sensenova";
    task.model = "sensenova/deepseek-v4-flash";
    task.effort = "high";
    task.output_schema_text = strictObjectSchemaTemplate();
    const workflow = buildWorkflow("demo", [task], providers, 1, "fail_fast");
    expect(workflow.tasks[0].provider_options).toEqual({
      model: "sensenova/deepseek-v4-flash",
      effort: "high",
    });
  });

  it("rejects non-strict Codex object schemas", () => {
    expect(() =>
      validateCodexOutputSchema(
        { type: "object", required: ["result"], properties: { result: { type: "string" } } },
        "task_1",
      ),
    ).toThrow("additionalProperties");
  });
});
