import type { CoordinatorEvent } from './types'

const DELEGATION_TERMINAL_STATUSES = new Set([
  'completed',
  'failed',
  'cancelled',
  'reconciliation_required',
  'waiting_input',
  'paused',
])

export function shouldDisplayCoordinatorEvent(event: CoordinatorEvent): boolean {
  if (event.event_type === 'session.created') return false
  if (event.event_type !== 'delegation.event') return true

  const source = asRecord(event.payload.source)
  const kind = stringValue(source?.kind)
  const status = stringValue(source?.status)
  if (status !== undefined && DELEGATION_TERMINAL_STATUSES.has(status)) return true
  if (kind === 'lifecycle' && status === 'active') return true
  if (kind !== 'output_completed') return false

  const providerPayload = asRecord(source?.payload)
  return (stringValue(providerPayload?.text)?.trim().length ?? 0) > 0
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}
