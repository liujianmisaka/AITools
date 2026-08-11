import { describe, expect, it } from "vitest";
import {
  forkImportedTemplate,
  MAX_WORKFLOW_IMPORT_BYTES,
  parseWorkflowTemplateJson,
  readWorkflowTemplateFile,
} from "./workflowImport";

const workflow = {
  id: "imported-flow",
  version: 7,
  name: "Imported flow",
  max_concurrency: 2,
  failure_policy: "fail_fast",
  tasks: [{ id: "one" }],
};

describe("workflow template import", () => {
  it("parses a workflow definition and resets it to the first template version", () => {
    const definition = parseWorkflowTemplateJson(JSON.stringify(workflow));

    expect(definition.id).toBe("imported-flow");
    expect(definition.version).toBe(1);
    expect(definition.tasks).toHaveLength(1);
  });

  it("rejects invalid JSON and definitions without tasks", () => {
    expect(() => parseWorkflowTemplateJson("{")).toThrow("不是有效的 JSON");
    expect(() =>
      parseWorkflowTemplateJson(JSON.stringify({ name: "empty", tasks: [] })),
    ).toThrow("至少需要一个任务");
  });

  it("rejects non-JSON files and oversized files", async () => {
    await expect(
      readWorkflowTemplateFile({
        name: "workflow.txt",
        size: 2,
        text: async () => "{}",
      }),
    ).rejects.toThrow(".json");
    await expect(
      readWorkflowTemplateFile({
        name: "workflow.json",
        size: MAX_WORKFLOW_IMPORT_BYTES + 1,
        text: async () => "{}",
      }),
    ).rejects.toThrow("1 MB");
  });

  it("removes a conflicting ID when importing as a new template", () => {
    const definition = parseWorkflowTemplateJson(JSON.stringify(workflow));
    const fork = forkImportedTemplate(definition);

    expect(fork.id).toBeUndefined();
    expect(fork.version).toBe(1);
    expect(fork.name).toContain("导入副本");
  });
});
