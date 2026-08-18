import type { Capability, Job, JobSubmission, ModelCatalog } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/api' + path, {
    headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || 'Request failed: ' + response.status)
  }
  return response.json() as Promise<T>
}

export const api = {
  jobs: () => request<Job[]>('/jobs'),
  job: (jobId: string) => request<Job>('/jobs/' + encodeURIComponent(jobId)),
  capabilities: () => request<Capability[]>('/capabilities'),
  models: () => request<ModelCatalog[]>('/models'),
  submit: (payload: JobSubmission) =>
    request<Job>('/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  cancel: (jobId: string) =>
    request<Job>('/jobs/' + encodeURIComponent(jobId) + '/cancel', { method: 'POST' }),
}
