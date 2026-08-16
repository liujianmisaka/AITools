import type {
  ApprovalRecord,
  InstanceDetail,
  InstanceRecord,
  JsonObject,
  ProviderCatalog,
  ScheduleRecord,
  TemplateRecord,
  TemplateVersionRecord,
  TriggerRecord,
  WorkflowDocument,
} from "../types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly payload: unknown,
  ) {
    super(`API request failed with HTTP ${status}`);
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  idempotencyKey?: string,
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);
  const response = await fetch(path, { ...init, headers });
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload as T;
}

const idempotencyKey = (scope: string) => `${scope}-${crypto.randomUUID()}`;

export const api = {
  health: () => request<{ status: string; streamHub: JsonObject }>("/health"),
  listTemplates: () => request<TemplateRecord[]>("/api/v2/templates"),
  getTemplate: (templateId: string) =>
    request<TemplateRecord>(`/api/v2/templates/${encodeURIComponent(templateId)}`),
  listTemplateVersions: (templateId: string) =>
    request<TemplateVersionRecord[]>(
      `/api/v2/templates/${encodeURIComponent(templateId)}/versions`,
    ),
  getTemplateVersion: (templateId: string, version: number) =>
    request<TemplateVersionRecord>(
      `/api/v2/templates/${encodeURIComponent(templateId)}/versions/${version}`,
    ),
  createTemplate: (definition: WorkflowDocument) =>
    request<TemplateRecord>(
      "/api/v2/templates",
      {
        method: "POST",
        body: JSON.stringify({
          templateId: definition.metadata.id,
          name: definition.metadata.name,
          description: definition.metadata.description ?? null,
        }),
      },
      idempotencyKey("template"),
    ),
  createTemplateVersion: (templateId: string, definition: WorkflowDocument) =>
    request<TemplateVersionRecord>(
      `/api/v2/templates/${encodeURIComponent(templateId)}/versions`,
      {
        method: "POST",
        body: JSON.stringify({ definition }),
      },
      idempotencyKey("version"),
    ),
  startInstance: (templateId: string, templateVersion: number, workflowInput: JsonObject) =>
    request<InstanceRecord>(
      `/api/v2/templates/${encodeURIComponent(templateId)}/instances`,
      {
        method: "POST",
        body: JSON.stringify({ templateVersion, workflowInput }),
      },
      idempotencyKey("instance"),
    ),
  listInstances: (statuses: string[] = []) => {
    const query = new URLSearchParams();
    statuses.forEach((status) => query.append("status", status));
    const suffix = query.size ? `?${query}` : "";
    return request<InstanceRecord[]>(`/api/v2/instances${suffix}`);
  },
  getInstance: (instanceId: string) =>
    request<InstanceDetail>(`/api/v2/instances/${encodeURIComponent(instanceId)}`),
  cancelInstance: (instanceId: string, reason: string) =>
    request<JsonObject>(
      `/api/v2/instances/${encodeURIComponent(instanceId)}/cancel`,
      { method: "POST", body: JSON.stringify({ reason }) },
      idempotencyKey("cancel"),
    ),
  listApprovals: (pendingOnly = false) =>
    request<ApprovalRecord[]>(`/api/v2/approvals?pending_only=${pendingOnly}`),
  decideApproval: (
    approvalId: string,
    decision: "approved" | "rejected",
    operatorLabel: string,
    reason: string,
  ) =>
    request<JsonObject>(
      `/api/v2/approvals/${encodeURIComponent(approvalId)}/decision`,
      {
        method: "POST",
        body: JSON.stringify({ decision, operatorLabel, reason }),
      },
      idempotencyKey("approval"),
    ),
  listTriggers: () => request<TriggerRecord[]>("/api/v2/triggers"),
  createTrigger: (payload: JsonObject) =>
    request<TriggerRecord>(
      "/api/v2/triggers",
      { method: "POST", body: JSON.stringify(payload) },
      idempotencyKey("trigger"),
    ),
  listSchedules: () => request<ScheduleRecord[]>("/api/v2/schedules"),
  createSchedule: (payload: JsonObject) =>
    request<ScheduleRecord>(
      "/api/v2/schedules",
      { method: "POST", body: JSON.stringify(payload) },
      idempotencyKey("schedule"),
    ),
  getCatalog: () => request<ProviderCatalog>("/api/v2/catalog/models"),
  getWorkspaces: () => request<string[]>("/api/v2/catalog/workspaces"),
};
