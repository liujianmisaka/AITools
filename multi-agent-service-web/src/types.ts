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

export type GroupActionResponse = {
  group_id: ServiceGroup
  action: ServiceAction
  services: ManagedService[]
}

export type ManagementConfiguration = {
  profile: string
  management_url: string
  service_web_url: string
  control_plane_url: string
  main_web_url: string
  workspace_ids: string[]
}
