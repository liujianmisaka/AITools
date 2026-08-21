import type { ManagedService, ServiceAction } from './types'

class ControlPlaneError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ControlPlaneError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/api' + path, {
    ...init,
    headers: { Accept: 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    throw new ControlPlaneError(await errorMessage(response), response.status)
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
  return response.statusText || 'Control Plane 请求失败 (' + response.status + ')'
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
  services: () => request<ManagedService[]>('/services'),
  changeServiceState: (serviceId: string, action: ServiceAction, epoch: number) =>
    request<ManagedService>(serviceActionPath(serviceId, action, epoch), { method: 'POST' }),
}
