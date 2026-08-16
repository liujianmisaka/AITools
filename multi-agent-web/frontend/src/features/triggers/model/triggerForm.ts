import type {
  TriggerBindingDefinition,
  TriggerBindingRecord,
  TriggerConcurrencyPolicy,
} from "../../../shared/types";

export interface TriggerEditorValues {
  id: string;
  name: string;
  source_type: string;
  event_type: string;
  event_version: number;
  template_id: string;
  enabled: boolean;
  source_key?: string;
  workspace_id?: string;
  remote?: string;
  branch?: string;
  fetch?: boolean;
  endpoint_key?: string;
  secret_ref?: string;
  signature_header?: string;
  signature_algorithm?: string;
  require_signature?: boolean;
  allowed_ip_cidrs?: string;
  max_payload_bytes?: number;
  dedup_header?: string;
  dedup_window_seconds?: number;
  event_filter_text: string;
  input_mapping_text: string;
  concurrency_policy: TriggerConcurrencyPolicy;
}

export function createTriggerId(): string {
  return `trigger_${crypto.randomUUID().replaceAll("-", "").slice(0, 20)}`;
}

export function parseJsonObject(
  value: string,
  label: string,
): Record<string, unknown> {
  const normalized = value.trim();
  if (!normalized) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(normalized);
  } catch {
    throw new Error(`${label}必须是有效的 JSON 对象`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${label}必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

export function parseStringMap(
  value: string,
  label: string,
): Record<string, string> {
  const parsed = parseJsonObject(value, label);
  if (Object.values(parsed).some((item) => typeof item !== "string")) {
    throw new Error(`${label}的所有值都必须是字符串路径`);
  }
  return parsed as Record<string, string>;
}

export function defaultEventTypeForSource(sourceType: string): string {
  if (sourceType === "git_commit") return "git.commit.updated";
  if (sourceType === "webhook") return "webhook.received";
  if (sourceType === "schedule") return "schedule.tick";
  if (sourceType === "internal") return "workflow.instance.status_changed";
  return "manual.event";
}

function parseCidrs(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function optionalString(value: string | undefined): string | null {
  const trimmed = value?.trim() ?? "";
  return trimmed || null;
}

export function triggerValuesToDefinition(
  values: TriggerEditorValues,
): TriggerBindingDefinition {
  const eventFilter = parseJsonObject(values.event_filter_text, "事件过滤");
  const inputMapping = parseStringMap(values.input_mapping_text, "输入映射");
  const isGit = values.source_type === "git_commit";
  const isWebhook = values.source_type === "webhook";
  const workspaceId = values.workspace_id?.trim() ?? "";
  const remote = values.remote?.trim() || "origin";
  const branch = values.branch?.trim() || "main";
  if (isGit && !workspaceId) {
    throw new Error("Git 提交源必须选择工作区");
  }
  let sourceConfig: Record<string, unknown>;
  let sourceKey: string | null;
  if (isGit) {
    sourceConfig = {
      workspace_id: workspaceId,
      remote,
      branch,
      fetch: values.fetch ?? true,
    };
    sourceKey = `${workspaceId}:${remote}:${branch}`;
  } else if (isWebhook) {
    const endpointKey = values.endpoint_key?.trim() ?? "";
    const secretRef = optionalString(values.secret_ref);
    if (!endpointKey) {
      throw new Error("Generic Webhook 必须填写 endpoint_key");
    }
    if (values.require_signature && !secretRef) {
      throw new Error("启用签名校验时必须填写 secret_ref");
    }
    sourceConfig = {
      endpoint_key: endpointKey,
      secret_ref: secretRef,
      signature_header: values.signature_header?.trim() || "x-hub-signature-256",
      signature_algorithm: values.signature_algorithm || "sha256",
      require_signature: values.require_signature ?? true,
      allowed_ip_cidrs: parseCidrs(values.allowed_ip_cidrs ?? ""),
      max_payload_bytes: values.max_payload_bytes ?? 1_048_576,
      dedup_header: optionalString(values.dedup_header),
      dedup_window_seconds: values.dedup_window_seconds ?? 3600,
    };
    sourceKey = endpointKey;
  } else {
    sourceConfig = {};
    sourceKey = values.source_key?.trim() || null;
  }
  return {
    id: values.id.trim(),
    name: values.name.trim(),
    source_type: values.source_type,
    event_type: values.event_type,
    event_version: values.event_version,
    source_key: sourceKey,
    template_id: values.template_id,
    enabled: values.enabled,
    source_config: sourceConfig,
    event_filter: eventFilter,
    input_mapping: inputMapping,
    concurrency_policy: values.concurrency_policy,
  };
}

export function triggerRecordToValues(
  record: TriggerBindingRecord,
): TriggerEditorValues {
  const config = record.source_config;
  return {
    id: record.id,
    name: record.name,
    source_type: record.source_type,
    event_type: record.event_type,
    event_version: record.event_version,
    template_id: record.template_id,
    enabled: record.enabled,
    source_key: record.source_key ?? "",
    workspace_id: String(config.workspace_id ?? ""),
    remote: String(config.remote ?? "origin"),
    branch: String(config.branch ?? "main"),
    fetch: Boolean(config.fetch ?? true),
    endpoint_key: String(config.endpoint_key ?? ""),
    secret_ref: String(config.secret_ref ?? ""),
    signature_header: String(config.signature_header ?? "x-hub-signature-256"),
    signature_algorithm: String(config.signature_algorithm ?? "sha256"),
    require_signature: Boolean(config.require_signature ?? true),
    allowed_ip_cidrs: Array.isArray(config.allowed_ip_cidrs)
      ? config.allowed_ip_cidrs.join(", ")
      : String(config.allowed_ip_cidrs ?? ""),
    max_payload_bytes: Number(config.max_payload_bytes ?? 1_048_576),
    dedup_header: config.dedup_header == null ? "" : String(config.dedup_header),
    dedup_window_seconds: Number(config.dedup_window_seconds ?? 3600),
    event_filter_text: JSON.stringify(record.event_filter, null, 2),
    input_mapping_text: JSON.stringify(record.input_mapping, null, 2),
    concurrency_policy: record.concurrency_policy,
  };
}

export function newTriggerValues(sourceType = "git_commit"): TriggerEditorValues {
  return {
    id: createTriggerId(),
    name: "",
    source_type: sourceType,
    event_type: defaultEventTypeForSource(sourceType),
    event_version: 1,
    template_id: "",
    enabled: true,
    source_key: "",
    workspace_id: "",
    remote: "origin",
    branch: "main",
    fetch: true,
    endpoint_key: "",
    secret_ref: "",
    signature_header: "x-hub-signature-256",
    signature_algorithm: "sha256",
    require_signature: true,
    allowed_ip_cidrs: "",
    max_payload_bytes: 1_048_576,
    dedup_header: "x-event-key",
    dedup_window_seconds: 3600,
    event_filter_text: "{}",
    input_mapping_text: "{}",
    concurrency_policy: "allow_parallel",
  };
}
