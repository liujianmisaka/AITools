export type AccessMode = "read_only" | "workspace_write";
export type SessionMode = "new" | "resume";
export type FailurePolicy = "continue_independent" | "fail_fast";

export interface ProviderModel {
  id: string;
  label: string;
  model_type: string;
  efforts: string[];
  default_effort?: string | null;
}

export interface ProviderCapabilities {
  read_only_mode?: boolean;
  workspace_write_mode?: boolean;
  structured_output?: boolean;
  resume_session?: boolean;
  cancel_running_turn?: boolean;
  [key: string]: boolean | undefined;
}

export interface ProviderDescription {
  name: string;
  started?: boolean;
  available?: boolean;
  models: ProviderModel[];
  capabilities: ProviderCapabilities;
  metadata?: Record<string, unknown>;
  error?: { code?: string; message?: string } | null;
}

export type WorkspaceMap = Record<string, string>;

export interface TaskDraft {
  id: string;
  depends_on: string[];
  provider: string;
  role: string;
  prompt_template: string;
  workspace_id: string;
  access: AccessMode;
  session_mode: SessionMode;
  provider_session_id: string;
  timeout_seconds: number;
  max_attempts: number;
  idempotent: boolean;
  model_type: string;
  model: string;
  effort: string;
  output_schema_text: string;
}

export interface TaskSpec {
  id: string;
  depends_on: string[];
  provider: string;
  role: string;
  prompt_template: string;
  workspace_id: string;
  access: AccessMode;
  session_mode: SessionMode;
  provider_session_id?: string;
  output_schema: Record<string, unknown> | null;
  timeout_seconds: number;
  retry_policy: { max_attempts: number; idempotent: boolean };
  provider_options: Record<string, unknown>;
}

export interface WorkflowDefinition {
  id?: string;
  version?: number;
  name: string;
  tasks: TaskSpec[];
  max_concurrency: number;
  failure_policy: FailurePolicy;
}

export interface WorkflowTemplateSummary {
  id: string;
  version: number;
  name: string;
  task_count: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface WorkflowTemplateRecord extends WorkflowTemplateSummary {
  definition: WorkflowDefinition;
}

export interface WorkflowTemplatePage {
  items: WorkflowTemplateSummary[];
  next_cursor: string | null;
}

export interface WorkflowTemplateValidation {
  valid: boolean;
  template_id: string;
  task_count: number;
}

export interface WorkflowInstanceSummary {
  id: string;
  template_id: string | null;
  template_version: number | null;
  source: "template" | "ad_hoc";
  name: string;
  task_count: number;
  completed_task_count: number;
  status: string;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkflowInstanceRecord extends WorkflowInstanceSummary {
  definition: WorkflowDefinition;
}

export interface WorkflowInstancePage {
  items: WorkflowInstanceSummary[];
  next_cursor: string | null;
}

export interface TaskInstanceRecord {
  id: string;
  workflow_instance_id: string;
  task_id: string;
  spec: TaskSpec;
  status: string;
  attempt_count: number;
  provider_session_id: string | null;
  final_output: unknown;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface EventSourceDescription {
  source_type: string;
  delivery_mode: "push" | "poll" | "hybrid";
  supports_polling: boolean;
  supports_push: boolean;
  external_push_enabled?: boolean;
  unique_source_key?: boolean;
  event_types?: string[];
  source_config_schema?: Record<string, unknown>;
  first_poll?: string;
}

export interface EventTypeDescription {
  event_type: string;
  version: number;
  description: string;
  source_types: string[];
  payload_schema: Record<string, unknown>;
}

export type TriggerConcurrencyPolicy = "allow_parallel" | "skip_if_running";

export interface TriggerBindingDefinition {
  id: string;
  name: string;
  source_type: string;
  event_type: string;
  event_version: number;
  source_key: string | null;
  template_id: string;
  enabled: boolean;
  source_config: Record<string, unknown>;
  event_filter: Record<string, unknown>;
  input_mapping: Record<string, string>;
  concurrency_policy: TriggerConcurrencyPolicy;
}

export interface TriggerBindingRecord extends TriggerBindingDefinition {
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface TriggerDeliveryRecord {
  id: string;
  trigger_event_id: string;
  trigger_binding_id: string;
  workflow_instance_id: string | null;
  status: string;
  reason: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface TriggerEventInput {
  source_type: string;
  event_type: string;
  event_version: number;
  source_key: string | null;
  dedup_key: string;
  payload: Record<string, unknown>;
}

export interface TriggerEventRecord extends TriggerEventInput {
  id: string;
  status: string;
  error: string | null;
  received_at: string;
  processed_at: string | null;
  deduplicated?: boolean;
  deliveries?: TriggerDeliveryRecord[];
}

export interface ScheduleTypeDescription {
  schedule_type: string;
  config_schema: Record<string, unknown>;
}

export interface ScheduledActionTypeDescription {
  action_type: string;
  config_schema: Record<string, unknown>;
}

export interface ScheduledTaskDefinition {
  id: string;
  version: number;
  name: string;
  schedule_type: string;
  schedule: Record<string, unknown>;
  action_type: string;
  action: Record<string, unknown>;
  enabled: boolean;
}

export interface ScheduledTaskRecord extends ScheduledTaskDefinition {
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  scheduler_error: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface ScheduledTaskRunRecord {
  id: string;
  scheduled_task_id: string;
  scheduled_for: string | null;
  status: string;
  result: Record<string, unknown> | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}
