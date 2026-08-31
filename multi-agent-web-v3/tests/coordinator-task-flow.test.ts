import assert from 'node:assert/strict'
import test from 'node:test'

import { projectCoordinatorTaskFlow } from '../src/coordinator-task-flow.ts'
import type {
  CoordinatorPlan,
  CoordinatorPlanGraph,
  CoordinatorPlanNode,
  Delegation,
} from '../src/types.ts'

const now = '2026-08-31T12:00:00Z'

function planNode(nodeId: string, status = 'proposed'): CoordinatorPlanNode {
  return {
    node_id: nodeId,
    intent: {
      task_id: nodeId,
      objective: `Objective ${nodeId}`,
      acceptance_criteria: [],
      required_capabilities: [],
      constraints: [],
      parent_task_id: null,
    },
    status,
    selection: null,
    execution: null,
    attempt: 1,
    created_at: now,
    updated_at: now,
  }
}

function plan(nodes: CoordinatorPlanNode[]): CoordinatorPlan {
  return {
    plan_id: 'plan-1',
    goal_id: 'goal-1',
    status: 'running',
    nodes,
    revision: 1,
    created_at: now,
    updated_at: now,
  }
}

function graph(
  dependencies: Array<{ node_id: string; depends_on_node_id: string }>,
): CoordinatorPlanGraph {
  return {
    plan_id: 'plan-1',
    revision: dependencies.length,
    dependencies,
    created_at: now,
    updated_at: now,
  }
}

function delegation(
  delegationId: string,
  status: string,
  childDelegationIds: string[] = [],
): Delegation {
  return {
    delegation_id: delegationId,
    status,
    revision: 1,
    session_id: null,
    channel_id: null,
    parent_delegation_id: null,
    depth: 0,
    child_scope: null,
    current_invocation_id: null,
    current_activation_id: null,
    activation_count: 1,
    child_delegation_ids: childDelegationIds,
    report: null,
  }
}

test('projects parallel tasks and dependency joins into deterministic stages', () => {
  const projection = projectCoordinatorTaskFlow(
    plan([
      planNode('backend'),
      planNode('frontend'),
      planNode('cross-check'),
      planNode('report'),
    ]),
    graph([
      { node_id: 'cross-check', depends_on_node_id: 'backend' },
      { node_id: 'cross-check', depends_on_node_id: 'frontend' },
      { node_id: 'report', depends_on_node_id: 'cross-check' },
    ]),
    {},
  )

  assert.deepEqual(
    projection.stages.map((stage) => stage.nodes.map((node) => node.nodeId)),
    [['backend', 'frontend'], ['cross-check'], ['report']],
  )
  assert.deepEqual(projection.edges, [
    { from: 'backend', to: 'cross-check' },
    { from: 'frontend', to: 'cross-check' },
    { from: 'cross-check', to: 'report' },
  ])
})

test('uses coordinator review state while retaining delegation child counts', () => {
  const backend = planNode('backend', 'review_required')
  backend.execution = {
    delegation_id: 'delegation-backend',
    activation_id: null,
    invocation_id: null,
    worker_session_id: 'worker-backend',
  }

  const projection = projectCoordinatorTaskFlow(plan([backend]), graph([]), {
    'delegation-backend': delegation(
      'delegation-backend',
      'completed',
      ['child-1', 'child-2'],
    ),
  })

  assert.equal(projection.nodes[0].status, 'review_required')
  assert.equal(projection.nodes[0].category, 'attention')
  assert.equal(projection.nodes[0].childDelegationCount, 2)
  assert.deepEqual(projection.summary, {
    total: 1,
    active: 0,
    completed: 0,
    attention: 1,
    pending: 0,
  })
})

test('uses the latest delegation failure over a stale coordinator review state', () => {
  const backend = planNode('backend', 'review_required')
  backend.execution = {
    delegation_id: 'delegation-backend',
    activation_id: null,
    invocation_id: null,
    worker_session_id: 'worker-backend',
  }

  const projection = projectCoordinatorTaskFlow(plan([backend]), graph([]), {
    'delegation-backend': delegation('delegation-backend', 'failed'),
  })

  assert.equal(projection.nodes[0].status, 'failed')
  assert.equal(projection.nodes[0].category, 'attention')
})

test('collapses large quiet plans and prioritizes active and attention stages', () => {
  const nodes = Array.from({ length: 9 }, (_, index) =>
    planNode(`task-${index + 1}`, index === 4 ? 'delegated' : index === 7 ? 'failed' : 'proposed'),
  )
  const dependencies = nodes.slice(1).map((node, index) => ({
    node_id: node.node_id,
    depends_on_node_id: nodes[index].node_id,
  }))

  const projection = projectCoordinatorTaskFlow(plan(nodes), graph(dependencies), {})

  assert.deepEqual(
    projection.stages.filter((stage) => stage.defaultExpanded).map((stage) => stage.index),
    [4, 7],
  )
})

test('rejects graph data that cannot represent the active plan', () => {
  const activePlan = plan([planNode('task-1')])
  assert.throws(
    () => projectCoordinatorTaskFlow(activePlan, { ...graph([]), plan_id: 'stale-plan' }, {}),
    /does not match/,
  )
  assert.throws(
    () => projectCoordinatorTaskFlow(
      activePlan,
      graph([{ node_id: 'missing', depends_on_node_id: 'task-1' }]),
      {},
    ),
    /unknown task/,
  )
})

test('projects an empty draft without inventing a task stage', () => {
  const projection = projectCoordinatorTaskFlow(plan([]), graph([]), {})

  assert.deepEqual(projection.stages, [])
  assert.deepEqual(projection.summary, {
    total: 0,
    active: 0,
    completed: 0,
    attention: 0,
    pending: 0,
  })
})
