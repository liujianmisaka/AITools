import type {
  Capability,
  CoordinatorActivationResponse,
  CoordinatorActivationSubmission,
  CoordinatorEvent,
  CoordinatorApprovalResponse,
  CoordinatorCancelResponse,
  CoordinatorNodeSnapshot,
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
  CreateTerminalSession,
  TerminalHostAccess,
  TerminalSession,
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

async function managementRequest<T>(path: string, init?: RequestInit): Promise<T> {
  return requestFromBase<T>('/management-api', path, init)
}

async function terminalHostRequest<T>(
  path: string,
  token: string,
  init?: RequestInit,
): Promise<T> {
  return requestFromBase<T>('/terminal-host', path, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) },
  })
}

export type DelegationActor = {
  actorId: string
  actorKind: string
}

export const delegationActor: DelegationActor = {
  actorId: import.meta.env.VITE_DELEGATION_ACTOR_ID ?? 'mcp-client',
  actorKind: import.meta.env.VITE_DELEGATION_ACTOR_KIND ?? 'application',
}

export const coordinatorDelegationActor: DelegationActor = {
  actorId: 'multi-agent-coordinator',
  actorKind: 'agent',
}

function delegationActorQuery(actor: DelegationActor = delegationActor) {
  return new URLSearchParams({
    actor_id: actor.actorId,
    actor_kind: actor.actorKind,
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
  delegations: (actor: DelegationActor = delegationActor) =>
    request<Delegation[]>('/delegations?' + delegationActorQuery(actor)),
  delegation: (delegationId: string, actor: DelegationActor = delegationActor) =>
    request<Delegation>(
      '/delegations/' + encodeURIComponent(delegationId) + '?' + delegationActorQuery(actor),
    ),
  delegationEvents: (
    delegationId: string,
    nextSequence = 1,
    actor: DelegationActor = delegationActor,
  ) =>
    request<InteractionMessage[]>(
      '/delegations/' +
        encodeURIComponent(delegationId) +
        '/events?' +
        delegationActorQuery(actor) +
        '&next_sequence=' +
        nextSequence,
    ),
  delegationSession: (delegationId: string, actor: DelegationActor = delegationActor) =>
    request<DelegationSession>(
      '/delegations/' + encodeURIComponent(delegationId) + '/session?' + delegationActorQuery(actor),
    ),
  delegationSessionEvents: (
    delegationId: string,
    nextSequence = 1,
    actor: DelegationActor = delegationActor,
  ) =>
    request<DelegationSessionEvent[]>(
      '/delegations/' +
        encodeURIComponent(delegationId) +
        '/session/events?' +
        delegationActorQuery(actor) +
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
  coordinatorNodeSnapshots: (sessionId: string) =>
    coordinatorRequest<CoordinatorNodeSnapshot[]>(
      '/coordinator/sessions/' +
        encodeURIComponent(sessionId) +
        '/node-snapshots',
    ),
  createCoordinatorSession: (payload: CoordinatorActivationSubmission) =>
    coordinatorRequest<CoordinatorActivationResponse>('/coordinator/sessions', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  sendCoordinatorMessage: (sessionId: string, message: string) =>
    coordinatorRequest<Record<string, unknown>>(
      '/coordinator/sessions/' + encodeURIComponent(sessionId) + '/messages',
      { method: 'POST', body: JSON.stringify({ message }) },
    ),
  cancelCoordinatorSession: (sessionId: string, reason: string) =>
    coordinatorRequest<CoordinatorCancelResponse>(
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
  coordinatorNodeMessage: (
    sessionId: string,
    nodeId: string,
    payload: {
      message: string
      delivery?: 'append' | 'interrupt_continue'
      expected_activation_id?: string
    },
  ) =>
    coordinatorRequest<Record<string, unknown>>(
      '/coordinator/sessions/' +
        encodeURIComponent(sessionId) +
        '/nodes/' +
        encodeURIComponent(nodeId) +
        '/messages',
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  coordinatorNodeContinue: (sessionId: string, nodeId: string, message: string) =>
    coordinatorRequest<Record<string, unknown>>(
      '/coordinator/sessions/' +
        encodeURIComponent(sessionId) +
        '/nodes/' +
        encodeURIComponent(nodeId) +
        '/continue',
      { method: 'POST', body: JSON.stringify({ message }) },
    ),
  coordinatorNodeReconcile: (
    sessionId: string,
    nodeId: string,
    payload: {
      expected_revision: number
      status: 'completed' | 'failed' | 'cancelled'
      reason: string
      output?: unknown
    },
  ) =>
    coordinatorRequest<Record<string, unknown>>(
      '/coordinator/sessions/' +
        encodeURIComponent(sessionId) +
        '/nodes/' +
        encodeURIComponent(nodeId) +
        '/reconcile',
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  coordinatorNodeAccept: (sessionId: string, nodeId: string, expectedSessionRevision: number) =>
    coordinatorRequest<Record<string, unknown>>(
      '/coordinator/sessions/' +
        encodeURIComponent(sessionId) +
        '/nodes/' +
        encodeURIComponent(nodeId) +
        '/accept',
      {
        method: 'POST',
        body: JSON.stringify({ expected_session_revision: expectedSessionRevision }),
      },
    ),
  coordinatorNodeRetry: (sessionId: string, nodeId: string) =>
    coordinatorRequest<Record<string, unknown>>(
      '/coordinator/sessions/' +
        encodeURIComponent(sessionId) +
        '/nodes/' +
        encodeURIComponent(nodeId) +
        '/retry',
      { method: 'POST', body: JSON.stringify({}) },
    ),
  terminalHostAccess: () => managementRequest<TerminalHostAccess>('/terminal-host/access'),
  terminalSessions: (delegationId: string, token: string) =>
    terminalHostRequest<{ sessions: TerminalSession[] }>(
      '/terminal-sessions?' +
        new URLSearchParams({ delegation_id: delegationId }).toString(),
      token,
    ).then((payload) => payload.sessions),
  createTerminalSession: (payload: CreateTerminalSession, token: string) =>
    terminalHostRequest<{ session: TerminalSession }>('/terminal-sessions', token, {
      method: 'POST',
      body: JSON.stringify(payload),
    }).then((response) => response.session),
}

export function terminalSessionStreamUrl(sessionId: string, clientId: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const query = new URLSearchParams({ client_id: clientId })
  return (
    protocol +
    '//' +
    window.location.host +
    '/terminal-host/terminal-sessions/' +
    encodeURIComponent(sessionId) +
    '/stream?' +
    query.toString()
  )
}

export function delegationEventsStreamUrl(
  delegationId: string,
  nextSequence: number,
  actor: DelegationActor = delegationActor,
): string {
  const params = new URLSearchParams({
    actor_id: actor.actorId,
    actor_kind: actor.actorKind,
    next_sequence: String(nextSequence),
  })
  return (
    '/api/delegations/' +
    encodeURIComponent(delegationId) +
    '/events/stream?' +
    params.toString()
  )
}

export function delegationSessionStreamUrl(
  delegationId: string,
  nextSequence: number,
  actor: DelegationActor = delegationActor,
): string {
  const params = new URLSearchParams({
    actor_id: actor.actorId,
    actor_kind: actor.actorKind,
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
