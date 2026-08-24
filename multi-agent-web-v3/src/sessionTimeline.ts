import type { DelegationSessionEvent } from './types'

export type AgentSessionItemKind =
  | 'message'
  | 'reasoning'
  | 'plan'
  | 'tool'
  | 'command'
  | 'file'
  | 'task'
  | 'status'

export type AgentSessionPlanEntry = {
  step: string
  status?: string
}

export type AgentSessionFileChange = {
  path: string
  kind?: string
}

export type AgentSessionItem = {
  key: string
  itemId: string
  kind: AgentSessionItemKind
  status?: string
  text: string
  name?: string
  command?: string
  stream?: string
  plan: AgentSessionPlanEntry[]
  changes: AgentSessionFileChange[]
  parentItemId?: string
  firstSequence: number
  lastSequence: number
  startedAt: string
  updatedAt: string
  completed: boolean
}

export type AgentSessionTurn = {
  key: string
  turnId: string
  activationNumber: number | null
  status?: string
  startedAt: string
  updatedAt: string
  completedAt?: string
  firstSequence: number
  lastSequence: number
  items: AgentSessionItem[]
}

type MutableTurn = Omit<AgentSessionTurn, 'items'> & {
  items: Map<string, AgentSessionItem>
}

const MAX_ITEM_TEXT_CHARS = 24_000
const DELTA_EVENT_KINDS = new Set([
  'output_delta',
  'reasoning_delta',
  'plan_delta',
  'tool_output_delta',
  'command_output_delta',
])

export function buildSessionTimeline(events: DelegationSessionEvent[]): AgentSessionTurn[] {
  const turns = new Map<string, MutableTurn>()

  for (const event of events) {
    const turnId = stringValue(event.payload.turn_id) ?? event.provider_operation_id
    if (!turnId && isUnscopedLifecycle(event.kind)) continue
    const stableTurnId = turnId ?? `activation-${event.activation_number ?? 0}`
    const turnKey = `${event.activation_number ?? 0}:${stableTurnId}`
    const turn = ensureTurn(turns, turnKey, stableTurnId, event)
    turn.lastSequence = event.sequence
    turn.updatedAt = event.occurred_at

    if (event.kind === 'turn_started') {
      turn.status = stringValue(event.payload.status) ?? 'in_progress'
      continue
    }
    if (event.kind === 'turn_completed') {
      turn.status = stringValue(event.payload.status) ?? event.status ?? 'completed'
      turn.completedAt = event.occurred_at
      continue
    }
    if (event.kind === 'terminal' || event.kind === 'cancelled' || event.kind === 'error') {
      turn.status = event.status ?? event.kind
    }

    const itemKind = sessionItemKind(event.kind)
    if (itemKind === null) continue
    const itemId = sessionItemId(event, itemKind)
    const itemKey = `${itemKind}:${itemId}`
    const item = ensureItem(turn, itemKey, itemId, itemKind, event)
    updateItem(item, event)
  }

  return [...turns.values()]
    .sort((left, right) => left.firstSequence - right.firstSequence)
    .map((turn) => ({
      ...turn,
      items: [...turn.items.values()].sort(
        (left, right) => left.firstSequence - right.firstSequence,
      ),
    }))
}

export function isSessionDeltaEvent(kind: string): boolean {
  return DELTA_EVENT_KINDS.has(kind)
}

function ensureTurn(
  turns: Map<string, MutableTurn>,
  key: string,
  turnId: string,
  event: DelegationSessionEvent,
): MutableTurn {
  const existing = turns.get(key)
  if (existing !== undefined) return existing
  const turn: MutableTurn = {
    key,
    turnId,
    activationNumber: event.activation_number,
    status: event.status ?? undefined,
    startedAt: event.occurred_at,
    updatedAt: event.occurred_at,
    firstSequence: event.sequence,
    lastSequence: event.sequence,
    items: new Map(),
  }
  turns.set(key, turn)
  return turn
}

function ensureItem(
  turn: MutableTurn,
  key: string,
  itemId: string,
  kind: AgentSessionItemKind,
  event: DelegationSessionEvent,
): AgentSessionItem {
  const existing = turn.items.get(key)
  if (existing !== undefined) return existing
  const item: AgentSessionItem = {
    key,
    itemId,
    kind,
    status: event.status ?? undefined,
    text: '',
    plan: [],
    changes: [],
    parentItemId:
      stringValue(event.payload.parent_item_id) ??
      stringValue(event.payload.parent_tool_use_id) ??
      undefined,
    firstSequence: event.sequence,
    lastSequence: event.sequence,
    startedAt: event.occurred_at,
    updatedAt: event.occurred_at,
    completed: false,
  }
  turn.items.set(key, item)
  return item
}

function updateItem(item: AgentSessionItem, event: DelegationSessionEvent): void {
  const text = stringValue(event.payload.text)
  if (text !== null) {
    item.text = isSessionDeltaEvent(event.kind)
      ? appendText(item.text, text)
      : text.slice(-MAX_ITEM_TEXT_CHARS)
  } else {
    const summary =
      stringValue(event.payload.summary) ?? stringValue(event.payload.error_message)
    if (summary !== null) item.text = summary.slice(-MAX_ITEM_TEXT_CHARS)
  }
  item.name =
    stringValue(event.payload.tool_name) ??
    stringValue(event.payload.name) ??
    stringValue(event.payload.tool) ??
    item.name
  item.command = stringValue(event.payload.command) ?? item.command
  item.stream = stringValue(event.payload.stream) ?? item.stream
  item.status = stringValue(event.payload.status) ?? event.status ?? item.status
  item.plan = planValue(event.payload.plan) ?? item.plan
  item.changes = fileChangesValue(event.payload.changes) ?? item.changes
  item.completed = item.completed || isCompletedKind(event.kind)
  item.lastSequence = event.sequence
  item.updatedAt = event.occurred_at
}

function appendText(current: string, chunk: string): string {
  return `${current}${chunk}`.slice(-MAX_ITEM_TEXT_CHARS)
}

function sessionItemKind(kind: string): AgentSessionItemKind | null {
  if (kind === 'output_delta' || kind === 'output_completed') return 'message'
  if (kind === 'reasoning_delta' || kind === 'reasoning_completed') return 'reasoning'
  if (kind === 'plan_delta' || kind === 'plan_completed') return 'plan'
  if (kind === 'tool_started' || kind === 'tool_output_delta' || kind === 'tool_completed') {
    return 'tool'
  }
  if (
    kind === 'command_started' ||
    kind === 'command_output_delta' ||
    kind === 'command_completed'
  ) {
    return 'command'
  }
  if (kind === 'file_changed') return 'file'
  if (kind === 'task_started' || kind === 'task_progress' || kind === 'task_completed') {
    return 'task'
  }
  if (kind === 'error' || kind === 'cancelled') return 'status'
  return null
}

function sessionItemId(
  event: DelegationSessionEvent,
  kind: AgentSessionItemKind,
): string {
  if (kind === 'plan') return 'plan'
  return (
    stringValue(event.payload.item_id) ??
    stringValue(event.payload.tool_use_id) ??
    `${kind}-${event.activation_number ?? 0}`
  )
}

function isCompletedKind(kind: string): boolean {
  return (
    kind.endsWith('_completed') ||
    kind === 'file_changed' ||
    kind === 'error' ||
    kind === 'cancelled'
  )
}

function isUnscopedLifecycle(kind: string): boolean {
  return kind === 'lifecycle' || kind === 'session_closed'
}

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function planValue(value: unknown): AgentSessionPlanEntry[] | null {
  if (!Array.isArray(value)) return null
  const plan = value.flatMap((entry) => {
    if (!isRecord(entry) || typeof entry.step !== 'string' || entry.step.length === 0) return []
    return [
      {
        step: entry.step,
        status: typeof entry.status === 'string' ? entry.status : undefined,
      },
    ]
  })
  return plan.length > 0 ? plan : null
}

function fileChangesValue(value: unknown): AgentSessionFileChange[] | null {
  if (!Array.isArray(value)) return null
  const changes = value.flatMap((entry) => {
    if (!isRecord(entry) || typeof entry.path !== 'string' || entry.path.length === 0) return []
    return [
      {
        path: entry.path,
        kind: typeof entry.kind === 'string' ? entry.kind : undefined,
      },
    ]
  })
  return changes.length > 0 ? changes : null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
