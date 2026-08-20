export type Job = {
  job_id: string
  idempotency_key: string
  status: string
  version: number
  request: Record<string, unknown>
  result: Record<string, unknown> | null
  error_code: string | null
  error_message: string | null
}

export type JobSubmission = {
  job_id: string
  idempotency_key: string
  capability_id: string
  operation: string
  input: Record<string, unknown>
  model: string
  effort: string
  network_policy?: 'allow' | 'deny'
  provider_id?: string
  output_schema?: Record<string, unknown>
}

export type Capability = {
  capability_id: string
  version: string
  operations: string[]
  features: string[]
}

export type Model = {
  model_id: string
  display_name: string
  description: string
  supported_efforts: string[]
}

export type ModelCatalog = {
  provider_id: string
  models: Model[]
}

export type TemplateNode = {
  node_id: string
  capability_id: string
  operation: string
  input: Record<string, unknown>
  model: string
  effort: string
  provider_id?: string
  output_schema?: Record<string, unknown>
  depends_on: string[]
}

export type Template = {
  template_id: string
  version: number
  name: string
  coordinator: 'direct' | 'dag'
  nodes: TemplateNode[]
  decision_required: boolean
  created_at: string
}

export type Instance = {
  instance_id: string
  idempotency_key: string
  template_id: string
  template_version: number
  status: string
  version: number
  input: Record<string, unknown>
  result: Record<string, unknown> | null
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export type Decision = {
  proposal_id: string
  revision: number
  instance_id: string
  plan_hash: string
  requested_effects: string[]
  scope_id: string
  status: string
  decided_by: string | null
  reason: string | null
  created_at: string
  decided_at: string | null
}

export type ManagedService = {
  service_id: string
  display_name: string
  description: string
  category: string
  status: 'stopped' | 'starting' | 'running' | 'stopping' | 'failed'
  controllable: boolean
  endpoint: string | null
  pid: number | null
  started_at: string | null
  last_error: string | null
  recent_output: string[]
}
