import type {
  CoordinatorPlan,
  CoordinatorPlanGraph,
  CoordinatorPlanNode,
  Delegation,
} from './types'

const ATTENTION_STATUSES = new Set([
  'failed',
  'reconciliation_required',
  'review_required',
])
const ACTIVE_STATUSES = new Set([
  'active',
  'awaiting_event',
  'delegated',
  'paused',
  'preparing',
  'reconciling',
  'reporting',
  'running',
  'waiting_input',
])
const COMPLETED_STATUSES = new Set(['accepted', 'completed', 'succeeded'])
export type CoordinatorTaskFlowCategory =
  | 'active'
  | 'attention'
  | 'cancelled'
  | 'completed'
  | 'pending'

export type CoordinatorTaskFlowNode = {
  node: CoordinatorPlanNode
  nodeId: string
  status: string
  category: CoordinatorTaskFlowCategory
  stage: number
  dependencies: string[]
  dependents: string[]
  childDelegationCount: number
}

export type CoordinatorTaskFlowEdge = {
  from: string
  to: string
}

export type CoordinatorTaskFlowStage = {
  index: number
  nodes: CoordinatorTaskFlowNode[]
  defaultExpanded: boolean
}

export type CoordinatorTaskFlowProjection = {
  nodes: CoordinatorTaskFlowNode[]
  edges: CoordinatorTaskFlowEdge[]
  stages: CoordinatorTaskFlowStage[]
  summary: {
    total: number
    active: number
    completed: number
    attention: number
    pending: number
  }
}

export function projectCoordinatorTaskFlow(
  plan: CoordinatorPlan,
  graph: CoordinatorPlanGraph | null,
  delegationSnapshots: Record<string, Delegation>,
  collapseThreshold = 8,
): CoordinatorTaskFlowProjection {
  const nodeIds = plan.nodes.map((node) => node.node_id)
  const nodeIdSet = new Set(nodeIds)
  const nodeOrder = new Map(nodeIds.map((nodeId, index) => [nodeId, index]))
  const dependencies = new Map(nodeIds.map((nodeId) => [nodeId, [] as string[]]))
  const dependents = new Map(nodeIds.map((nodeId) => [nodeId, [] as string[]]))
  const edges: CoordinatorTaskFlowEdge[] = []

  if (graph !== null && graph.plan_id !== plan.plan_id) {
    throw new Error('Coordinator task graph does not match the active plan')
  }

  for (const dependency of graph?.dependencies ?? []) {
    if (!nodeIdSet.has(dependency.node_id) || !nodeIdSet.has(dependency.depends_on_node_id)) {
      throw new Error('Coordinator task graph references an unknown task')
    }
    dependencies.get(dependency.node_id)!.push(dependency.depends_on_node_id)
    dependents.get(dependency.depends_on_node_id)!.push(dependency.node_id)
    edges.push({ from: dependency.depends_on_node_id, to: dependency.node_id })
  }

  const ready = nodeIds.filter((nodeId) => dependencies.get(nodeId)!.length === 0)
  const stageByNode = new Map<string, number>()
  const remainingDependencyCount = new Map(
    nodeIds.map((nodeId) => [nodeId, dependencies.get(nodeId)!.length]),
  )
  let visitedCount = 0

  while (ready.length > 0) {
    ready.sort((left, right) => nodeOrder.get(left)! - nodeOrder.get(right)!)
    const nodeId = ready.shift()!
    const dependencyStages = dependencies.get(nodeId)!.map(
      (dependencyId) => stageByNode.get(dependencyId)!,
    )
    stageByNode.set(
      nodeId,
      dependencyStages.length === 0 ? 0 : Math.max(...dependencyStages) + 1,
    )
    visitedCount += 1

    for (const dependentId of dependents.get(nodeId)!) {
      const remaining = remainingDependencyCount.get(dependentId)! - 1
      remainingDependencyCount.set(dependentId, remaining)
      if (remaining === 0) ready.push(dependentId)
    }
  }

  if (visitedCount !== plan.nodes.length) {
    throw new Error('Coordinator task graph contains a cycle')
  }

  const projectedNodes = plan.nodes.map((node): CoordinatorTaskFlowNode => {
    const delegation = node.execution
      ? delegationSnapshots[node.execution.delegation_id]
      : undefined
    const status = effectiveTaskStatus(node, delegation)
    return {
      node,
      nodeId: node.node_id,
      status,
      category: taskStatusCategory(status),
      stage: stageByNode.get(node.node_id)!,
      dependencies: dependencies.get(node.node_id)!,
      dependents: dependents.get(node.node_id)!,
      childDelegationCount: delegation?.child_delegation_ids.length ?? 0,
    }
  })
  const stageCount = projectedNodes.length === 0
    ? 0
    : Math.max(...projectedNodes.map((node) => node.stage)) + 1
  const stages = Array.from({ length: stageCount }, (_, index) => ({
    index,
    nodes: projectedNodes.filter((node) => node.stage === index),
    defaultExpanded: plan.nodes.length <= collapseThreshold,
  }))

  if (plan.nodes.length > collapseThreshold) {
    const prioritizedStages = stages.filter((stage) =>
      stage.nodes.some((node) => node.category === 'active' || node.category === 'attention'),
    )
    const stagesToExpand = prioritizedStages.length > 0
      ? prioritizedStages
      : [
          stages.find((stage) => stage.nodes.some((node) => node.category === 'pending'))
            ?? stages.at(-1)!,
        ]
    for (const stage of stagesToExpand) stage.defaultExpanded = true
  }

  return {
    nodes: projectedNodes,
    edges,
    stages,
    summary: {
      total: projectedNodes.length,
      active: countCategory(projectedNodes, 'active'),
      completed: countCategory(projectedNodes, 'completed'),
      attention: countCategory(projectedNodes, 'attention'),
      pending: countCategory(projectedNodes, 'pending'),
    },
  }
}

export function taskStatusCategory(status: string): CoordinatorTaskFlowCategory {
  if (ATTENTION_STATUSES.has(status)) return 'attention'
  if (ACTIVE_STATUSES.has(status)) return 'active'
  if (COMPLETED_STATUSES.has(status)) return 'completed'
  if (status === 'cancelled') return 'cancelled'
  return 'pending'
}

function effectiveTaskStatus(node: CoordinatorPlanNode, delegation?: Delegation): string {
  if (node.status === 'accepted') return node.status
  if (node.status === 'review_required' && delegation?.status === 'completed') {
    return node.status
  }
  return delegation?.status ?? node.status
}

function countCategory(
  nodes: CoordinatorTaskFlowNode[],
  category: CoordinatorTaskFlowCategory,
): number {
  return nodes.filter((node) => node.category === category).length
}
