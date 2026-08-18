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
