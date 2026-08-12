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

export function triggerValuesToDefinition(
  values: TriggerEditorValues,
): TriggerBindingDefinition {
  const eventFilter = parseJsonObject(values.event_filter_text, "事件过滤");
  const inputMapping = parseStringMap(values.input_mapping_text, "输入映射");
  const isGit = values.source_type === "git_commit";
  const workspaceId = values.workspace_id?.trim() ?? "";
  const remote = values.remote?.trim() || "origin";
  const branch = values.branch?.trim() || "main";
  if (isGit && !workspaceId) {
    throw new Error("Git 提交源必须选择工作区");
  }
  const sourceKey = isGit
    ? `${workspaceId}:${remote}:${branch}`
    : values.source_key?.trim() || null;
  return {
    id: values.id.trim(),
    name: values.name.trim(),
    source_type: values.source_type,
    event_type: values.event_type,
    event_version: values.event_version,
    source_key: sourceKey,
    template_id: values.template_id,
    enabled: values.enabled,
    source_config: isGit
      ? {
          workspace_id: workspaceId,
          remote,
          branch,
          fetch: values.fetch ?? true,
        }
      : {},
    event_filter: eventFilter,
    input_mapping: inputMapping,
    concurrency_policy: values.concurrency_policy,
  };
}

export function triggerRecordToValues(
  record: TriggerBindingRecord,
): TriggerEditorValues {
  return {
    id: record.id,
    name: record.name,
    source_type: record.source_type,
    event_type: record.event_type,
    event_version: record.event_version,
    template_id: record.template_id,
    enabled: record.enabled,
    source_key: record.source_key ?? "",
    workspace_id: String(record.source_config.workspace_id ?? ""),
    remote: String(record.source_config.remote ?? "origin"),
    branch: String(record.source_config.branch ?? "main"),
    fetch: Boolean(record.source_config.fetch ?? true),
    event_filter_text: JSON.stringify(record.event_filter, null, 2),
    input_mapping_text: JSON.stringify(record.input_mapping, null, 2),
    concurrency_policy: record.concurrency_policy,
  };
}

export function newTriggerValues(
  sourceType = "git_commit",
): TriggerEditorValues {
  return {
    id: createTriggerId(),
    name: "",
    source_type: sourceType,
    event_type:
      sourceType === "git_commit" ? "git.commit.updated" : "manual.event",
    event_version: 1,
    template_id: "",
    enabled: true,
    source_key: "",
    workspace_id: "",
    remote: "origin",
    branch: "main",
    fetch: true,
    event_filter_text: "{}",
    input_mapping_text: "{}",
    concurrency_policy: "allow_parallel",
  };
}
