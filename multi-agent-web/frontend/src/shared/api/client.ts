import type {
  ProviderDescription,
  TaskInstanceRecord,
  WorkflowDefinition,
  WorkflowInstancePage,
  WorkflowInstanceRecord,
  WorkflowTemplatePage,
  WorkflowTemplateRecord,
  WorkflowTemplateValidation,
  WorkspaceMap,
} from "../types";

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(message: string, status: number, code = "request_failed") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export async function requestJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError("服务返回了无法解析的响应", response.status, "invalid_response");
  }
  if (!response.ok) {
    const body = payload as { detail?: unknown; code?: string };
    const detail = Array.isArray(body.detail)
      ? body.detail
          .map((item) => {
            if (typeof item === "object" && item && "msg" in item) {
              return String((item as { msg: unknown }).msg);
            }
            return JSON.stringify(item);
          })
          .join("；")
      : String(body.detail ?? body.code ?? "请求失败");
    throw new ApiError(detail, response.status, body.code);
  }
  return payload as T;
}

export const coreApi = {
  health: () => requestJson<{ status: string }>("/api/core/health"),
  providers: () => requestJson<ProviderDescription[]>("/api/providers"),
  workspaces: () => requestJson<WorkspaceMap>("/api/workspaces"),

  validateTemplate: (definition: WorkflowDefinition) =>
    requestJson<WorkflowTemplateValidation>("/api/templates/validate", {
      method: "POST",
      body: JSON.stringify(definition),
    }),
  listTemplates: (options: {
    limit?: number;
    cursor?: string;
    includeArchived?: boolean;
  } = {}) => {
    const params = new URLSearchParams({
      limit: String(options.limit ?? 50),
      include_archived: String(options.includeArchived ?? false),
    });
    if (options.cursor) params.set("cursor", options.cursor);
    return requestJson<WorkflowTemplatePage>(`/api/templates?${params.toString()}`);
  },
  getTemplate: (templateId: string) =>
    requestJson<WorkflowTemplateRecord>(
      `/api/templates/${encodeURIComponent(templateId)}`,
    ),
  createTemplate: (definition: WorkflowDefinition) =>
    requestJson<WorkflowTemplateRecord>("/api/templates", {
      method: "POST",
      body: JSON.stringify(definition),
    }),
  updateTemplate: (templateId: string, definition: WorkflowDefinition) =>
    requestJson<WorkflowTemplateRecord>(
      `/api/templates/${encodeURIComponent(templateId)}`,
      { method: "PUT", body: JSON.stringify(definition) },
    ),
  archiveTemplate: (templateId: string) =>
    requestJson<WorkflowTemplateRecord>(
      `/api/templates/${encodeURIComponent(templateId)}`,
      { method: "DELETE" },
    ),
  instantiateTemplate: (templateId: string) =>
    requestJson<WorkflowInstanceRecord>(
      `/api/templates/${encodeURIComponent(templateId)}/instances`,
      { method: "POST" },
    ),

  createAdHocInstance: (definition: WorkflowDefinition) =>
    requestJson<WorkflowInstanceRecord>("/api/instances", {
      method: "POST",
      body: JSON.stringify(definition),
    }),
  listInstances: (options: {
    limit?: number;
    cursor?: string;
    status?: string;
  } = {}) => {
    const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
    if (options.cursor) params.set("cursor", options.cursor);
    if (options.status) params.set("status", options.status);
    return requestJson<WorkflowInstancePage>(`/api/instances?${params.toString()}`);
  },
  getInstance: (instanceId: string) =>
    requestJson<WorkflowInstanceRecord>(
      `/api/instances/${encodeURIComponent(instanceId)}`,
    ),
  getTaskInstances: (instanceId: string) =>
    requestJson<TaskInstanceRecord[]>(
      `/api/instances/${encodeURIComponent(instanceId)}/tasks`,
    ),
  cancelInstance: (instanceId: string) =>
    requestJson<WorkflowInstanceRecord>(
      `/api/instances/${encodeURIComponent(instanceId)}/cancel`,
      { method: "POST" },
    ),
};
