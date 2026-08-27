import type {
  Capability,
  CoordinatorActivationSubmission,
  CoordinatorEvent,
  CoordinatorApprovalResponse,
  CoordinatorSession,
  CoordinatorSessionSummary,
  Decision,
  Delegation,
  DelegationSession,
  DelegationSessionEvent,
  Instance,
  InteractionMessage,
  Job,
  JobSubmission,
  ManagedService,
  MessageDispatch,
  MessageDispatchSubmission,
  ModelCatalog,
  Template,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return requestFromBase<T>('/api', path, init)
}

async function requestFromBase<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(base + path, {
    headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || 'Request failed: ' + response.status)
  }
  return response.json() as Promise<T>
}

async function coordinatorRequest<T>(path: string, init?: RequestInit): Promise<T> {
  return requestFromBase<T>('/coordinator-api', path, init)
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
  dispatchDelegationMessage: (
    delegationId: string,
    payload: MessageDispatchSubmission,
  ) =>
    request<MessageDispatch>(
      '/delegations/' + encodeURIComponent(delegationId) + '/messages/dispatch',
      { method: 'POST', body: JSON.stringify(payload) },
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
  coordinatorSessions: () =>
    coordinatorRequest<{ sessions: CoordinatorSessionSummary[] }>('/coordinator/sessions').then(
      (payload) => payload.sessions,
    ),
  coordinatorSession: (sessionId: string) =>
    coordinatorRequest<CoordinatorSession>(
      '/coordinator/sessions/' + encodeURIComponent(sessionId),
    ),
  coordinatorEvents: (sessionId: string, nextSequence = 1) =>
    coordinatorRequest<CoordinatorEvent[]>(
      '/coordinator/sessions/' +
        encodeURIComponent(sessionId) +
        '/events?next_sequence=' +
        nextSequence,
    ),
  createCoordinatorSession: (payload: CoordinatorActivationSubmission) =>
    coordinatorRequest<Record<string, unknown>>('/coordinator/sessions', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  sendCoordinatorMessage: (sessionId: string, message: string) =>
    coordinatorRequest<Record<string, unknown>>(
      '/coordinator/sessions/' + encodeURIComponent(sessionId) + '/messages',
      { method: 'POST', body: JSON.stringify({ message }) },
    ),
  cancelCoordinatorSession: (sessionId: string, reason: string) =>
    coordinatorRequest<CoordinatorSession>(
      '/coordinator/sessions/' + encodeURIComponent(sessionId) + '/cancel',
      { method: 'POST', body: JSON.stringify({ reason }) },
    ),
  resolveCoordinatorApproval: (
    sessionId: string,
    approvalId: string,
    payload: { approved: boolean; actor_id: string; reason: string; expected_session_revision: number },
  ) =>
    coordinatorRequest<CoordinatorApprovalResponse>(
      '/coordinator/sessions/' +
        encodeURIComponent(sessionId) +
        '/approvals/' +
        encodeURIComponent(approvalId),
      { method: 'POST', body: JSON.stringify(payload) },
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

export function coordinatorStreamUrl(sessionId: string, nextSequence: number): string {
  return (
    '/coordinator-api/coordinator/sessions/' +
    encodeURIComponent(sessionId) +
    '/stream?next_sequence=' +
    encodeURIComponent(String(nextSequence))
  )
}
