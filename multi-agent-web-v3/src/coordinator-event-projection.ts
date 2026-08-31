import type { CoordinatorEvent } from './types'

export function shouldDisplayCoordinatorEvent(event: CoordinatorEvent): boolean {
  if (event.event_type === 'session.created') return false
  return event.event_type !== 'delegation.event'
}

export function coordinatorTaskFlowInsertionIndex(events: CoordinatorEvent[]): number {
  let insertionIndex = 0
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index]
    if (event.event_type !== 'coordinator.decision') continue
    const decision = asRecord(event.payload.decision)
    const kind = typeof decision?.kind === 'string' ? decision.kind : undefined
    if (kind === 'create_plan' || kind === 'revise_plan') insertionIndex = index + 1
  }
  return insertionIndex
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}
