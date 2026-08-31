import assert from 'node:assert/strict'
import test from 'node:test'

import {
  coordinatorTaskFlowInsertionIndex,
  shouldDisplayCoordinatorEvent,
} from '../src/coordinator-event-projection.ts'
import type { CoordinatorEvent } from '../src/types.ts'

function coordinatorEvent(
  eventType: string,
  payload: Record<string, unknown>,
): CoordinatorEvent {
  return {
    schema_version: 1,
    session_id: 'coordinator-1',
    sequence: 1,
    event_id: 'event-1',
    event_type: eventType,
    payload,
    occurred_at: '2026-08-31T16:00:00Z',
  }
}

function delegationEvent(
  kind: string,
  status: string,
  providerPayload: Record<string, unknown> = {},
): CoordinatorEvent {
  return coordinatorEvent('delegation.event', {
    node_id: 'task-1',
    delegation_id: 'delegation-1',
    source: { kind, status, payload: providerPayload },
  })
}

test('hides delegation events because task state is projected into one live flow card', () => {
  assert.equal(shouldDisplayCoordinatorEvent(delegationEvent('output_delta', 'running')), false)
  assert.equal(
    shouldDisplayCoordinatorEvent(delegationEvent('lifecycle', 'preflighting')),
    false,
  )
  assert.equal(shouldDisplayCoordinatorEvent(delegationEvent('command_completed', 'running')), false)
  assert.equal(
    shouldDisplayCoordinatorEvent(
      delegationEvent('output_completed', 'running', { text: '已完成第一阶段检查。' }),
    ),
    false,
  )
  assert.equal(shouldDisplayCoordinatorEvent(delegationEvent('lifecycle', 'failed')), false)
})

test('hides duplicate session creation but preserves coordinator conversation events', () => {
  assert.equal(shouldDisplayCoordinatorEvent(coordinatorEvent('session.created', {})), false)
  assert.equal(
    shouldDisplayCoordinatorEvent(coordinatorEvent('coordinator.decision', {})),
    true,
  )
  assert.equal(shouldDisplayCoordinatorEvent(coordinatorEvent('activation.failed', {})), true)
})

test('places the live task flow after the latest plan decision', () => {
  const events = [
    coordinatorEvent('user.message', {}),
    coordinatorEvent('coordinator.decision', { decision: { kind: 'create_plan' } }),
    coordinatorEvent('coordinator.decision', { decision: { kind: 'delegate' } }),
    coordinatorEvent('coordinator.decision', { decision: { kind: 'revise_plan' } }),
    coordinatorEvent('activation.completed', {}),
  ]

  assert.equal(coordinatorTaskFlowInsertionIndex(events), 4)
  assert.equal(coordinatorTaskFlowInsertionIndex([coordinatorEvent('user.message', {})]), 0)
})
