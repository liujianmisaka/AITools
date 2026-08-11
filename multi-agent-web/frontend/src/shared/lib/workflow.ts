import { z } from "zod";
import type {
  ProviderDescription,
  TaskDraft,
  TaskSpec,
  WorkflowDefinition,
} from "../types";

export const identifierPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

export const taskDraftSchema = z.object({
  id: z
    .string()
    .min(1, "请输入任务 ID")
    .regex(identifierPattern, "只能包含字母、数字、点、下划线和连字符"),
  provider: z.string().min(1, "请选择 Provider"),
  role: z.string().min(1, "请输入任务角色").max(100),
  workspace_id: z.string().min(1, "请选择工作区"),
  access: z.enum(["read_only", "workspace_write"]),
  prompt_template: z.string().min(1, "请输入任务提示词"),
});

export function availableProviderNames(providers: ProviderDescription[]): string[] {
  return providers.filter((provider) => provider.available !== false).map((provider) => provider.name);
}

export function modelsForProvider(
  providers: ProviderDescription[],
  providerName: string,
) {
  return (
    providers.find(
      (provider) => provider.name === providerName && provider.available !== false,
    )?.models ?? []
  );
}

export function createTaskDraft(
  providers: ProviderDescription[],
  workspaceIds: string[],
  id = "task_1",
): TaskDraft {
  const providerNames = availableProviderNames(providers);
  return {
    id,
    depends_on: [],
    provider: providerNames.includes("codex") ? "codex" : (providerNames[0] ?? ""),
    role: "worker",
    prompt_template: "",
    workspace_id: workspaceIds[0] ?? "",
    access: "read_only",
    session_mode: "new",
    provider_session_id: "",
    timeout_seconds: 300,
    max_attempts: 1,
    idempotent: false,
    model_type: "",
    model: "",
    effort: "",
    output_schema_text: "",
  };
}

function schemaFailure(taskId: string, path: string, message: string): never {
  throw new Error(`Codex 任务 ${taskId} 的输出 Schema：${path} ${message}`);
}

export function validateCodexOutputSchema(
  schema: Record<string, unknown>,
  taskId: string,
): void {
  function visit(node: unknown, path: string, requireObject: boolean): void {
    if (!node || typeof node !== "object" || Array.isArray(node)) {
      schemaFailure(taskId, path, "必须是 JSON Schema 对象");
    }
    const record = node as Record<string, unknown>;
    const nodeTypes = Array.isArray(record.type) ? record.type : [record.type];
    const isObject = nodeTypes.includes("object") || Object.hasOwn(record, "properties");
    if (requireObject && !isObject) schemaFailure(taskId, path, "根节点 type 必须为 object");

    if (isObject) {
      const properties = record.properties;
      if (!properties || typeof properties !== "object" || Array.isArray(properties)) {
        schemaFailure(taskId, `${path}.properties`, "必须是对象");
      }
      if (record.additionalProperties !== false) {
        schemaFailure(taskId, `${path}.additionalProperties`, "必须显式设置为 false");
      }
      if (!Array.isArray(record.required)) {
        schemaFailure(taskId, `${path}.required`, "必须列出 properties 中的所有字段");
      }
      const missing = Object.keys(properties as object).filter(
        (name) => !(record.required as unknown[]).includes(name),
      );
      if (missing.length) {
        schemaFailure(taskId, `${path}.required`, `缺少字段：${missing.join(", ")}`);
      }
      Object.entries(properties as Record<string, unknown>).forEach(([name, child]) => {
        visit(child, `${path}.properties.${name}`, false);
      });
    }

    if (nodeTypes.includes("array")) {
      if (!record.items || typeof record.items !== "object" || Array.isArray(record.items)) {
        schemaFailure(taskId, `${path}.items`, "数组必须声明 items");
      }
      visit(record.items, `${path}.items`, false);
    }

    if (record.$defs !== undefined) {
      if (!record.$defs || typeof record.$defs !== "object" || Array.isArray(record.$defs)) {
        schemaFailure(taskId, `${path}.$defs`, "必须是对象");
      }
      Object.entries(record.$defs as Record<string, unknown>).forEach(([name, child]) => {
        visit(child, `${path}.$defs.${name}`, false);
      });
    }

    (["anyOf", "oneOf", "allOf"] as const).forEach((keyword) => {
      if (record[keyword] === undefined) return;
      if (!Array.isArray(record[keyword])) {
        schemaFailure(taskId, `${path}.${keyword}`, "必须是数组");
      }
      (record[keyword] as unknown[]).forEach((child, index) => {
        visit(child, `${path}.${keyword}[${index}]`, false);
      });
    });
  }

  visit(schema, "$", true);
}

function parseOutputSchema(task: TaskDraft): Record<string, unknown> | null {
  const source = task.output_schema_text.trim();
  if (!source) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch {
    throw new Error(`任务 ${task.id} 的输出 Schema 不是有效 JSON`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`任务 ${task.id} 的输出 Schema 必须是 JSON 对象`);
  }
  if (task.provider === "codex") {
    validateCodexOutputSchema(parsed as Record<string, unknown>, task.id);
  }
  return parsed as Record<string, unknown>;
}

function taskToSpec(
  task: TaskDraft,
  providers: ProviderDescription[],
  knownIds: Set<string>,
): TaskSpec {
  const parsed = taskDraftSchema.safeParse(task);
  if (!parsed.success) {
    throw new Error(`任务 ${task.id || "未命名"}：${parsed.error.issues[0]?.message ?? "配置无效"}`);
  }
  task.depends_on.forEach((dependency) => {
    if (!knownIds.has(dependency)) throw new Error(`任务 ${task.id} 引用了未知依赖 ${dependency}`);
    if (dependency === task.id) throw new Error(`任务不能依赖自身：${task.id}`);
  });

  const providerOptions: Record<string, unknown> = {};
  const models = modelsForProvider(providers, task.provider);
  if (task.provider === "codex" || models.length > 0) {
    if (!task.model_type) throw new Error(`任务 ${task.id} 必须选择模型类型`);
    const selectedModel = models.find(
      (model) => model.id === task.model && model.model_type === task.model_type,
    );
    if (!selectedModel) throw new Error(`任务 ${task.id} 必须从目录选择模型`);
    if (!task.effort) throw new Error(`任务 ${task.id} 必须显式选择推理等级`);
    if (selectedModel.efforts.length && !selectedModel.efforts.includes(task.effort)) {
      throw new Error(`任务 ${task.id} 的推理等级不适用于所选模型`);
    }
    providerOptions.model = selectedModel.id;
    if (task.provider === "codex") providerOptions.effort = task.effort;
    else providerOptions.reasoning_effort = task.effort;
  }

  if (task.access === "workspace_write" && task.max_attempts > 1 && !task.idempotent) {
    throw new Error(`写入任务 ${task.id} 重试时必须声明幂等`);
  }
  if (task.session_mode === "resume" && !task.provider_session_id.trim()) {
    throw new Error(`任务 ${task.id} 恢复会话时必须填写 Provider Session ID`);
  }

  return {
    id: task.id.trim(),
    depends_on: [...task.depends_on],
    provider: task.provider,
    role: task.role.trim(),
    prompt_template: task.prompt_template.trim(),
    workspace_id: task.workspace_id,
    access: task.access,
    session_mode: task.session_mode,
    ...(task.session_mode === "resume"
      ? { provider_session_id: task.provider_session_id.trim() }
      : {}),
    output_schema: parseOutputSchema(task),
    timeout_seconds: Number(task.timeout_seconds) || 300,
    retry_policy: {
      max_attempts: Number(task.max_attempts) || 1,
      idempotent: task.idempotent,
    },
    provider_options: providerOptions,
  };
}

export function buildWorkflow(
  name: string,
  tasks: TaskDraft[],
  providers: ProviderDescription[],
  maxConcurrency: number,
  failurePolicy: WorkflowDefinition["failure_policy"],
  identity?: { id: string | null; version: number },
): WorkflowDefinition {
  const normalizedName = name.trim();
  if (!normalizedName) throw new Error("工作流名称不能为空");
  if (!tasks.length) throw new Error("至少需要一个任务");
  const ids = tasks.map((task) => task.id.trim());
  if (new Set(ids).size !== ids.length) throw new Error("任务 ID 不能重复");
  ids.forEach((id) => {
    if (!identifierPattern.test(id)) throw new Error(`任务 ID 格式无效：${id}`);
  });
  const knownIds = new Set(ids);
  return {
    ...(identity?.id ? { id: identity.id, version: identity.version } : {}),
    name: normalizedName,
    tasks: tasks.map((task) => taskToSpec(task, providers, knownIds)),
    max_concurrency: Math.min(64, Math.max(1, Number(maxConcurrency) || 1)),
    failure_policy: failurePolicy,
  };
}

export function specToDraft(
  task: TaskSpec,
  providers: ProviderDescription[] = [],
): TaskDraft {
  const options = task.provider_options ?? {};
  const effort = String(options.effort ?? options.reasoning_effort ?? "");
  const model = String(options.model ?? "");
  const configuredModel = modelsForProvider(providers, task.provider).find(
    (candidate) => candidate.id === model,
  );
  const modelType = configuredModel?.model_type ?? (model.includes("/") ? model.split("/", 1)[0] : "");
  return {
    id: task.id,
    depends_on: [...task.depends_on],
    provider: task.provider,
    role: task.role,
    prompt_template: task.prompt_template,
    workspace_id: task.workspace_id,
    access: task.access,
    session_mode: task.session_mode ?? "new",
    provider_session_id: task.provider_session_id ?? "",
    timeout_seconds: task.timeout_seconds,
    max_attempts: task.retry_policy.max_attempts,
    idempotent: task.retry_policy.idempotent,
    model_type: modelType,
    model,
    effort,
    output_schema_text: task.output_schema ? JSON.stringify(task.output_schema, null, 2) : "",
  };
}

export function strictObjectSchemaTemplate(): string {
  return JSON.stringify(
    {
      type: "object",
      required: ["result"],
      properties: { result: { type: "string" } },
      additionalProperties: false,
    },
    null,
    2,
  );
}
