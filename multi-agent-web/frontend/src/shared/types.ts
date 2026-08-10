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

export interface WorkflowValidation {
  valid: boolean;
  workflow_id: string;
  task_count: number;
}

export interface RunRecord {
  id: string;
  workflow_id?: string;
  workflow?: WorkflowDefinition;
  status: string;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface TaskRunRecord {
  id?: string;
  run_id?: string;
  task_id: string;
  spec?: TaskSpec;
  status: string;
  attempt_count?: number;
  provider_session_id?: string | null;
  final_output?: unknown;
  error_code?: string | null;
  error_message?: string | null;
  created_at?: string;
  updated_at?: string;
}
