import type {
  ProviderDescription,
  RunRecord,
  TaskRunRecord,
  WorkflowDefinition,
  WorkflowValidation,
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
  validateWorkflow: (workflow: WorkflowDefinition) =>
    requestJson<WorkflowValidation>("/api/workflows/validate", {
      method: "POST",
      body: JSON.stringify(workflow),
    }),
  createRun: (workflow: WorkflowDefinition) =>
    requestJson<RunRecord>("/api/runs", {
      method: "POST",
      body: JSON.stringify(workflow),
    }),
  getRun: (runId: string) => requestJson<RunRecord>(`/api/runs/${runId}`),
  getTasks: (runId: string) =>
    requestJson<TaskRunRecord[]>(`/api/runs/${runId}/tasks`),
  cancelRun: (runId: string) =>
    requestJson<RunRecord>(`/api/runs/${runId}/cancel`, { method: "POST" }),
};
