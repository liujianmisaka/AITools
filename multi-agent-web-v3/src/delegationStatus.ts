const TERMINAL_DELEGATION_STATUSES = new Set([
  'completed',
  'rejected',
  'failed',
  'cancelled',
])

export function isTerminalDelegationStatus(status: string): boolean {
  return TERMINAL_DELEGATION_STATUSES.has(status)
}
