import type { Capability, Decision, Delegation, Instance, Job, JobSubmission, ManagedService, ModelCatalog, Template } from './types'

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

export const delegationActor = {
  actorId: import.meta.env.VITE_DELEGATION_ACTOR_ID ?? 'mcp-client',
  actorKind: import.meta.env.VITE_DELEGATION_ACTOR_KIND ?? 'application',
}

function delegationActorQuery() {
  return new URLSearchParams({
    actor_id: delegationActor.actorId,
    actor_kind: delegationActor.actorKind,
  }).toString()
}

export const api = {
  jobs: () => request<Job[]>('/jobs'),
  job: (jobId: string) => request<Job>('/jobs/' + encodeURIComponent(jobId)),
  capabilities: () => request<Capability[]>('/capabilities'),
  models: () => request<ModelCatalog[]>('/models'),
  templates: () => request<Template[]>('/templates'),
  instances: () => request<Instance[]>('/instances'),
  decisions: () => request<Decision[]>('/decisions'),
  services: () => request<ManagedService[]>('/services'),
  delegations: () => request<Delegation[]>('/delegations?' + delegationActorQuery()),
  startService: (serviceId: string) =>
    request<ManagedService>('/services/' + encodeURIComponent(serviceId) + '/start', { method: 'POST' }),
  stopService: (serviceId: string) =>
    request<ManagedService>('/services/' + encodeURIComponent(serviceId) + '/stop', { method: 'POST' }),
  submit: (payload: JobSubmission) =>
    request<Job>('/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  cancel: (jobId: string) =>
    request<Job>('/jobs/' + encodeURIComponent(jobId) + '/cancel', { method: 'POST' }),
  decide: (proposalId: string, revision: number, decision: 'approved' | 'rejected') =>
    request<Decision>('/decisions/' + encodeURIComponent(proposalId) + '/revisions/' + revision + '/decision', {
      method: 'POST',
      body: JSON.stringify({ decision, principal_id: 'local-user' }),
    }),
}
