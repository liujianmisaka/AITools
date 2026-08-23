import type {
  GroupActionResponse,
  ManagedService,
  ManagementConfiguration,
  ManagementConfigurationUpdate,
  DirectoryPickerResponse,
  ServiceAction,
  ServiceGroup,
} from './types'

class ManagementApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ManagementApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/api' + path, {
    ...init,
    headers: { Accept: 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    throw new ManagementApiError(await errorMessage(response), response.status)
  }
  return response.json() as Promise<T>
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const payload: unknown = await response.json()
    if (
      typeof payload === 'object' &&
      payload !== null &&
      'detail' in payload &&
      typeof payload.detail === 'string'
    ) {
      return payload.detail
    }
  } catch {
    // The status text remains a useful fallback for non-JSON proxy failures.
  }
  return response.statusText || 'AITools Management API 请求失败 (' + response.status + ')'
}

export function serviceActionPath(
  serviceId: string,
  action: ServiceAction,
  epoch: number,
): string {
  const query = new URLSearchParams({ epoch: String(epoch) })
  return '/services/' + encodeURIComponent(serviceId) + '/' + action + '?' + query.toString()
}

export const api = {
  configuration: () => request<ManagementConfiguration>('/configuration'),
  selectDirectory: (initialPath: string | null) =>
    request<DirectoryPickerResponse>('/configuration/select-directory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initial_path: initialPath }),
    }),
  updateConfiguration: (configuration: ManagementConfigurationUpdate) =>
    request<ManagementConfiguration>('/configuration', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(configuration),
    }),
  services: () => request<ManagedService[]>('/services'),
  changeServiceState: (serviceId: string, action: ServiceAction, epoch: number) =>
    request<ManagedService>(serviceActionPath(serviceId, action, epoch), { method: 'POST' }),
  changeGroup: (groupId: ServiceGroup, action: ServiceAction) =>
    request<GroupActionResponse>('/groups/' + groupId + '/' + action, { method: 'POST' }),
}
