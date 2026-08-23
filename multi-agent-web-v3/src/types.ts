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

export type DelegationReport = {
  status: string
  output: unknown
  artifact_ids: string[]
  error_code: string | null
  error_message: string | null
  source_invocation_id: string | null
  source_activation_id: string | null
  created_at: string
}

export type Delegation = {
  delegation_id: string
  status: string
  revision: number
  session_id: string | null
  channel_id: string | null
  parent_delegation_id: string | null
  depth: number
  child_scope: {
    scope_id: string
    parent_scope_id: string | null
  } | null
  current_invocation_id: string | null
  current_activation_id: string | null
  activation_count: number
  child_delegation_ids: string[]
  report: DelegationReport | null
}

export type InteractionPrincipal = {
  principal_id: string
  kind: string
  display_name: string
}

export type InteractionMessage = {
  message_id: string
  channel_id: string
  sender: InteractionPrincipal
  recipient: InteractionPrincipal | null
  message_type: string
  payload: Record<string, unknown>
  sequence: number
  scope: {
    scope_id: string
    parent_scope_id: string | null
  }
  correlation_id: string | null
  causation_id: string | null
  reply_to: string | null
  delivery_status: string
  created_at: string
}
