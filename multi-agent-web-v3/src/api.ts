import type {
  Capability,
  Decision,
  Delegation,
  DelegationSession,
  DelegationSessionEvent,
  Instance,
  InteractionMessage,
  Job,
  JobSubmission,
  ManagedService,
  ModelCatalog,
  Template,
} from './types'

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
  delegation: (delegationId: string) =>
    request<Delegation>(
      '/delegations/' + encodeURIComponent(delegationId) + '?' + delegationActorQuery(),
    ),
  delegationEvents: (delegationId: string, nextSequence = 1) =>
    request<InteractionMessage[]>(
      '/delegations/' +
        encodeURIComponent(delegationId) +
        '/events?' +
        delegationActorQuery() +
        '&next_sequence=' +
        nextSequence,
    ),
  delegationSession: (delegationId: string) =>
    request<DelegationSession>(
      '/delegations/' + encodeURIComponent(delegationId) + '/session?' + delegationActorQuery(),
    ),
  delegationSessionEvents: (delegationId: string, nextSequence = 1) =>
    request<DelegationSessionEvent[]>(
      '/delegations/' +
        encodeURIComponent(delegationId) +
        '/session/events?' +
        delegationActorQuery() +
        '&next_sequence=' +
        nextSequence,
    ),
  resolveDelegationReconciliation: (
    delegationId: string,
    payload: {
      request_id: string
      idempotency_key: string
      actor: { principal_id: string; kind: string }
      expected_revision: number
      status: 'completed' | 'failed' | 'cancelled'
      reason: string
      output: unknown
    },
  ) =>
    request<Delegation>(
      '/delegations/' + encodeURIComponent(delegationId) + '/reconciliation/resolve',
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  startService: (serviceId: string) =>
    request<ManagedService>(
      '/services/' + encodeURIComponent(serviceId) + '/start',
      { method: 'POST' },
    ),
  stopService: (serviceId: string) =>
    request<ManagedService>(
      '/services/' + encodeURIComponent(serviceId) + '/stop',
      { method: 'POST' },
    ),
  submit: (payload: JobSubmission) =>
    request<Job>('/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  cancel: (jobId: string) =>
    request<Job>('/jobs/' + encodeURIComponent(jobId) + '/cancel', { method: 'POST' }),
  decide: (proposalId: string, revision: number, decision: 'approved' | 'rejected') =>
    request<Decision>(
      '/decisions/' + encodeURIComponent(proposalId) + '/revisions/' + revision + '/decision',
      {
        method: 'POST',
        body: JSON.stringify({ decision, principal_id: 'local-user' }),
      },
    ),
}

export function delegationEventsStreamUrl(delegationId: string, nextSequence: number): string {
  const params = new URLSearchParams({
    actor_id: delegationActor.actorId,
    actor_kind: delegationActor.actorKind,
    next_sequence: String(nextSequence),
  })
  return (
    '/api/delegations/' +
    encodeURIComponent(delegationId) +
    '/events/stream?' +
    params.toString()
  )
}

export function delegationSessionStreamUrl(delegationId: string, nextSequence: number): string {
  const params = new URLSearchParams({
    actor_id: delegationActor.actorId,
    actor_kind: delegationActor.actorKind,
    next_sequence: String(nextSequence),
  })
  return (
    '/api/delegations/' +
    encodeURIComponent(delegationId) +
    '/session/stream?' +
    params.toString()
  )
}
