export type JsonObject = Record<string, unknown>;

export interface TemplateRecord {
  templateId: string;
  name: string;
  description?: string | null;
  latestVersion: number;
  revision: number;
  createdAt: string;
  updatedAt: string;
}

export interface TemplateVersionRecord {
  templateId: string;
  version: number;
  definition: WorkflowDocument;
  compiledPlan: JsonObject;
  planHash: string;
  catalogRevision: string;
  createdAt: string;
}

export interface CatalogModel {
  id: string;
  label: string;
  modelType: string;
  efforts: string[];
  recommendedEffort?: string | null;
}

export interface ProviderCatalog {
  runtimeName: string;
  runtimeId: string;
  providerId: string;
  revision: string;
  models: CatalogModel[];
  updatedAt: string;
}

export interface InstanceRecord {
  instanceId: string;
  templateId: string;
  templateVersion: number;
  temporalWorkflowId: string;
  temporalRunId?: string | null;
  status: string;
  workflowInput: JsonObject;
  output?: JsonObject | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  triggerCause?: JsonObject | null;
  projectionVersion: number;
  createdAt: string;
  updatedAt: string;
  startedAt?: string | null;
  completedAt?: string | null;
}

export interface NodeProjection {
  instanceId: string;
  nodeId: string;
  activation: number;
  executionId?: string | null;
  status: string;
  output?: JsonObject | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  projectionVersion: number;
  updatedAt: string;
}

export interface ApprovalRecord {
  approvalId: string;
  instanceId: string;
  nodeId: string;
  activation: number;
  label: string;
  status: string;
  commandId?: string | null;
  operatorLabel?: string | null;
  reason?: string | null;
  requestedAt: string;
  decidedAt?: string | null;
}

export interface InstanceDetail {
  instance: InstanceRecord;
  nodes: NodeProjection[];
  approvals: ApprovalRecord[];
}

export interface TriggerRecord {
  triggerId: string;
  name: string;
  revision: number;
  enabled: boolean;
  eventType: string;
  sourcePattern?: string | null;
  subjectPattern?: string | null;
  templateId: string;
  templateVersion: number;
  inputBindings: Record<string, string>;
  createdAt: string;
  updatedAt: string;
}

export interface ScheduleRecord {
  scheduleId: string;
  name: string;
  revision: number;
  enabled: boolean;
  scheduleKind: "cron" | "interval" | "calendar";
  scheduleSpec: JsonObject;
  targetKind: "workflow" | "git_connector";
  target: JsonObject;
  createdAt: string;
  updatedAt: string;
}

export interface WorkflowMetadata {
  id: string;
  version: number;
  name: string;
  description?: string;
}

export interface WorkflowTransition {
  id: string;
  from: string;
  to: string;
  on: string;
  priority: number;
  condition?: string;
}

export interface AgentExecution {
  provider: string;
  model: string;
  effort: string;
  workspaceId: string;
  access: "read_only" | "workspace_write";
  sessionMode: "new" | "resume";
  instruction: string;
  timeout: string;
  retry: { maximumAttempts: number };
}

export interface WorkflowNode {
  id: string;
  type: string;
  typeVersion: number;
  inputs: Array<{ name: string; expression: string }>;
  outputSchema: JsonObject;
  agent?: AgentExecution;
  activity?: JsonObject;
  approval?: JsonObject;
  timer?: JsonObject;
  waitEvent?: JsonObject;
  decision?: JsonObject;
  join?: JsonObject;
}

export interface WorkflowDocument {
  apiVersion: "orchestration.misaka.dev/v1";
  kind: "Workflow";
  metadata: WorkflowMetadata;
  spec: {
    flow: { type: "dag" | "state_machine" };
    inputSchema: JsonObject;
    outputSchema: JsonObject;
    failurePolicy: "fail_fast" | "continue_independent";
    maxConcurrency: number;
    nodes: WorkflowNode[];
    transitions: WorkflowTransition[];
    outputs: Array<{ name: string; expression: string }>;
  };
}
