import { z } from "zod";
import type { WorkflowDefinition } from "../types";

export const MAX_WORKFLOW_IMPORT_BYTES = 1024 * 1024;

const workflowImportSchema = z
  .object({
    id: z.string().optional(),
    version: z.number().int().positive().optional(),
    name: z.string().min(1, "工作流名称不能为空"),
    tasks: z.array(z.unknown()).min(1, "工作流至少需要一个任务"),
    max_concurrency: z.number().int().positive().optional(),
    failure_policy: z
      .enum(["continue_independent", "fail_fast"])
      .optional(),
  })
  .passthrough();

export class WorkflowImportError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WorkflowImportError";
  }
}

export function parseWorkflowTemplateJson(source: string): WorkflowDefinition {
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch {
    throw new WorkflowImportError("文件不是有效的 JSON");
  }

  const parsed = workflowImportSchema.safeParse(value);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const path = issue.path.length ? `${issue.path.join(".")}：` : "";
    throw new WorkflowImportError(`${path}${issue.message}`);
  }

  return {
    ...parsed.data,
    version: 1,
  } as WorkflowDefinition;
}

export async function readWorkflowTemplateFile(
  file: Pick<File, "name" | "size" | "text">,
): Promise<WorkflowDefinition> {
  if (!file.name.toLowerCase().endsWith(".json")) {
    throw new WorkflowImportError("请选择 .json 工作流文件");
  }
  if (file.size > MAX_WORKFLOW_IMPORT_BYTES) {
    throw new WorkflowImportError("工作流 JSON 文件不能超过 1 MB");
  }
  return parseWorkflowTemplateJson(await file.text());
}

export function forkImportedTemplate(
  definition: WorkflowDefinition,
): WorkflowDefinition {
  const copy = { ...definition };
  delete copy.id;
  return {
    ...copy,
    version: 1,
    name: `${definition.name}（导入副本）`,
  };
}
