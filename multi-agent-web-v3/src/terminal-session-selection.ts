import type { TerminalRuntime, TerminalSession } from './types'

export const ACTIVE_TERMINAL_STATUSES: ReadonlySet<TerminalSession['status']> = new Set([
  'starting',
  'running',
])

export function selectActiveTerminalSession(
  sessions: readonly TerminalSession[],
  providerId: string,
  providerSessionId: string,
  runtime: TerminalRuntime,
): TerminalSession | null {
  return (
    sessions
      .filter(
        (session) =>
          session.provider_id === providerId &&
          session.provider_session_id === providerSessionId &&
          session.runtime === runtime &&
          ACTIVE_TERMINAL_STATUSES.has(session.status),
      )
      .sort((left, right) => right.created_at.localeCompare(left.created_at))[0] ?? null
  )
}
