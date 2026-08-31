import assert from 'node:assert/strict'
import test from 'node:test'

import { selectActiveTerminalSession } from '../src/terminal-session-selection.ts'
import type { TerminalSession, TerminalSessionStatus } from '../src/types.ts'

function terminalSession(
  id: string,
  status: TerminalSessionStatus,
  createdAt: string,
  overrides: Partial<TerminalSession> = {},
): TerminalSession {
  return {
    id,
    delegation_id: 'delegation-1',
    provider_id: 'claude',
    provider_session_id: 'provider-session-1',
    runtime: 'claude',
    cwd: 'D:\\dev\\AITools',
    cols: 120,
    rows: 34,
    status,
    sequence: 0,
    created_at: createdAt,
    updated_at: createdAt,
    exit_code: null,
    exit_signal: null,
    last_error: null,
    input_lease: null,
    ...overrides,
  }
}

test('ignores a stale terminal session left by a previous host process', () => {
  const selected = selectActiveTerminalSession(
    [terminalSession('stale', 'failed', '2026-08-31T16:21:07.902Z')],
    'claude',
    'provider-session-1',
    'claude',
  )

  assert.equal(selected, null)
})

test('selects the newest matching active terminal session', () => {
  const selected = selectActiveTerminalSession(
    [
      terminalSession('older-running', 'running', '2026-08-31T16:21:07.902Z'),
      terminalSession('newer-running', 'running', '2026-08-31T17:21:07.902Z'),
      terminalSession('other-provider', 'running', '2026-08-31T18:21:07.902Z', {
        provider_id: 'codex',
        runtime: 'codex',
      }),
    ],
    'claude',
    'provider-session-1',
    'claude',
  )

  assert.equal(selected?.id, 'newer-running')
})
