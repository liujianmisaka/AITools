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
  approval_required: boolean
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

export type Approval = {
  approval_id: string
  instance_id: string
  status: string
  decision: 'approve' | 'reject' | null
  reason: string | null
  created_at: string
  decided_at: string | null
}
