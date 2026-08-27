export type ServiceStatus =
  | 'stopped'
  | 'starting'
  | 'running'
  | 'stopping'
  | 'failed'
  | 'unavailable'
  | 'on_demand'

export type ServiceScope = 'aitools' | 'control_plane' | 'client'
export type LaunchMode = 'managed' | 'delegated' | 'on_demand'

export type ManagedService = {
  service_id: string
  display_name: string
  description: string
  category: string
  scope: ServiceScope
  launch_mode: LaunchMode
  status: ServiceStatus
  controllable: boolean
  available: boolean
  endpoint: string | null
  pid: number | null
  process_create_time: number | null
  epoch: number
  started_at: string | null
  stopped_at: string | null
  exit_code: number | null
  last_error: string | null
  recent_output: string[]
  depends_on: string[]
}

export type ServiceAction = 'start' | 'stop'

export type ServiceActionRequest = {
  service: ManagedService
  action: ServiceAction
}

export type ServiceGroup = 'core' | 'all'
export type ProviderKind = 'fake' | 'codex' | 'claude'
export type ClaudeRuntimeMode = 'native' | 'opencodex'
export type CoordinatorReasoningEffort = 'none' | 'low' | 'medium' | 'high' | 'xhigh'

export type GroupActionResponse = {
  group_id: ServiceGroup
  action: ServiceAction
  services: ManagedService[]
}

export type ProviderConfiguration = {
  provider_id: string
  kind: ProviderKind
  codex_home: string | null
  config_overrides: string[]
  claude_config_dir: string | null
  claude_cli_path: string | null
  model_ids: string[]
  network_deny_enforced: boolean
}

export type ManagementConfiguration = {
  providers: ProviderConfiguration[]
  allowed_path_roots: string[]
  claude_runtime_mode: ClaudeRuntimeMode
  claude_opencodex_base_url: string
  claude_opencodex_auth_token_env: string
  coordinator_model: string
  coordinator_reasoning_effort: CoordinatorReasoningEffort
  coordinator_api_key_env: string
  coordinator_base_url: string | null
  coordinator_max_decision_steps: number
  coordinator_wait_timeout_ms: number
  management_url: string
  service_web_url: string
  control_plane_url: string
  main_web_url: string
  coordinator_url: string
}

export type ManagementConfigurationUpdate = Pick<
  ManagementConfiguration,
  | 'providers'
  | 'allowed_path_roots'
  | 'claude_runtime_mode'
  | 'claude_opencodex_base_url'
  | 'claude_opencodex_auth_token_env'
  | 'coordinator_model'
  | 'coordinator_reasoning_effort'
  | 'coordinator_api_key_env'
  | 'coordinator_base_url'
  | 'coordinator_max_decision_steps'
  | 'coordinator_wait_timeout_ms'
>

export type DirectoryPickerResponse = {
  path: string | null
}
