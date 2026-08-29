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
  resolution_reason: string | null
  resolved_by: {
    principal_id: string
    kind: string
    display_name: string
  } | null
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

export type DelegationSessionEvent = {
  delegation_id: string
  sequence: number
  kind: string
  invocation_id: string | null
  activation_id: string | null
  activation_number: number | null
  status: string | null
  provider_session_id: string | null
  provider_operation_id: string | null
  payload: Record<string, unknown>
  occurred_at: string
}

export type DelegationSession = {
  delegation: Delegation
  provider_id: string | null
  model: string | null
  effort: string | null
  provider_session_id: string | null
  provider_operation_id: string | null
  activation_number: number
  last_sequence: number
  stage: string | null
  closed: boolean
  updated_at: string | null
}

export type MessageDispatch = {
  dispatch_id: string
  delegation_id: string
  session_id: string
  status: string
  revision: number
  applied_strategy: string | null
  previous_activation_id: string | null
  current_activation_id: string | null
  error_code: string | null
  error_message: string | null
  updated_at: string | null
}

export type MessageDispatchSubmission = {
  dispatch_id: string
  idempotency_key: string
  actor: { principal_id: string; kind: string }
  session_id: string
  expected_activation_id?: string
  delivery: 'append' | 'interrupt_continue'
  message_id: string
  message_type: 'instruction' | 'answer'
  payload: Record<string, unknown>
  correlation_id?: string
  reply_to?: string
  model?: string
  effort?: string
}

export type CoordinatorGoal = {
  goal_id: string
  objective: string
  acceptance_criteria: string[]
  constraints: string[]
  status: string
  created_at: string
  updated_at: string
}

export type CoordinatorPlanNode = {
  node_id: string
  intent: {
    task_id: string
    objective: string
    acceptance_criteria: string[]
    required_capabilities: string[]
    constraints: string[]
    parent_task_id: string | null
  }
  status: string
  selection: {
    provider_id: string
    model_id: string
    effort: string | null
    rationale: string
    capability_ids: string[]
  } | null
  execution: {
    delegation_id: string
    activation_id: string | null
    invocation_id: string | null
    worker_session_id: string | null
  } | null
  attempt: number
  created_at: string
  updated_at: string
}

export type CoordinatorNodeSnapshot = {
  node_id: string
  snapshot: Delegation
}

export type CoordinatorPlan = {
  plan_id: string
  goal_id: string
  status: string
  nodes: CoordinatorPlanNode[]
  revision: number
  created_at: string
  updated_at: string
}

export type CoordinatorPlanGraph = {
  plan_id: string
  revision: number
  dependencies: Array<{
    node_id: string
    depends_on: string[]
  }>
  updated_at: string
}

export type CoordinatorSessionDomain = {
  schema_version: number
  session_id: string
  cognitive_session_id: string
  goal: CoordinatorGoal | null
  plan: CoordinatorPlan | null
  plan_graph: CoordinatorPlanGraph | null
  last_event_id: string | null
  last_event_at: string | null
  revision: number
  created_at: string
  updated_at: string
  autonomy: {
    approvals: Array<Record<string, unknown>>
    [key: string]: unknown
  }
  plan_revisions: Array<Record<string, unknown>>
}

export type CoordinatorSession = {
  session: CoordinatorSessionDomain
  cognitive_session_id: string
  working_directory: string | null
}

export type CoordinatorSessionSummary = {
  session_id: string
  revision: number
  goal: CoordinatorGoal | null
  plan_status: string | null
  updated_at: string
  working_directory: string | null
}

export type CoordinatorApprovalResponse = {
  session: CoordinatorSessionDomain
  approval: Record<string, unknown>
}

export type CoordinatorCancelResponse = {
  session: CoordinatorSessionDomain
}

export type CoordinatorEvent = {
  schema_version: number
  session_id: string
  sequence: number
  event_id: string
  event_type: string
  payload: Record<string, unknown>
  occurred_at: string
}

export type CoordinatorActivationSubmission = {
  session_id: string
  prompt: string
  cwd: string
  cognitive_session_id?: string
  acceptance_criteria?: string[]
  constraints?: string[]
}
