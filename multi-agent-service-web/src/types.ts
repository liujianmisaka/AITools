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
export type ProviderKind = 'fake' | 'codex'

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
  network_deny_enforced: boolean
}

export type ManagementConfiguration = {
  providers: ProviderConfiguration[]
  allowed_path_roots: string[]
  management_url: string
  service_web_url: string
  control_plane_url: string
  main_web_url: string
}

export type ManagementConfigurationUpdate = Pick<
  ManagementConfiguration,
  'providers' | 'allowed_path_roots'
>

export type DirectoryPickerResponse = {
  path: string | null
}
