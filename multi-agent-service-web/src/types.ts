export type ServiceStatus = 'stopped' | 'starting' | 'running' | 'stopping' | 'failed'

export type ManagedService = {
  service_id: string
  display_name: string
  description: string
  category: string
  status: ServiceStatus
  controllable: boolean
  endpoint: string | null
  pid: number | null
  process_create_time: number | null
  epoch: number
  started_at: string | null
  stopped_at: string | null
  exit_code: number | null
  last_error: string | null
  recent_output: string[]
}

export type ServiceAction = 'start' | 'stop'

export type ServiceActionRequest = {
  service: ManagedService
  action: ServiceAction
}
