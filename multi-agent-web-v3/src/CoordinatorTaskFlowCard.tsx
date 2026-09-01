import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from 'react'
import { ChevronDown, GitBranch, Layers3, ShieldCheck } from 'lucide-react'

import { api } from './api'
import { CoordinatorStatus } from './coordinator-status'
import {
  projectCoordinatorTaskFlow,
  type CoordinatorTaskFlowNode,
  type CoordinatorTaskFlowProjection,
  type CoordinatorTaskFlowStage,
} from './coordinator-task-flow'
import { CoordinatorTaskFlowNodeCard } from './CoordinatorTaskFlowNodeCard'
import type { CoordinatorPlan, CoordinatorSessionDomain, Delegation } from './types'

type CoordinatorTaskFlowCardProps = {
  session: CoordinatorSessionDomain
  delegationSnapshots: Record<string, Delegation>
  onOpenDelegation: (delegationId: string) => void
  onChanged: () => void
  collapsed?: boolean
  onToggleCollapsed?: () => void
}

type TaskFlowPath = {
  key: string
  data: string
}

export function CoordinatorTaskFlowCard(props: CoordinatorTaskFlowCardProps) {
  if (props.session.plan === null) {
    return (
      <article className={'coordinator-task-flow-card empty ' + (props.collapsed ? 'collapsed' : '')}>
        <TaskFlowHeader title="当前任务流" description="Coordinator 尚未形成任务计划" collapsed={props.collapsed} onToggleCollapsed={props.onToggleCollapsed} />
        {!props.collapsed && (
          <div className="coordinator-task-flow-empty">
            <GitBranch size={20} />
            <strong>等待 Coordinator 拆解任务</strong>
            <span>计划形成后，依赖关系和执行状态会在这张卡片中持续更新。</span>
          </div>
        )}
      </article>
    )
  }

  return <CoordinatorTaskFlowGraph {...props} plan={props.session.plan} />
}

function CoordinatorTaskFlowGraph({
  session,
  plan,
  delegationSnapshots,
  onOpenDelegation,
  onChanged,
  collapsed = false,
  onToggleCollapsed,
}: CoordinatorTaskFlowCardProps & { plan: CoordinatorPlan }) {
  const projection = useMemo(
    () => projectCoordinatorTaskFlow(plan, session.plan_graph, delegationSnapshots),
    [delegationSnapshots, plan, session.plan_graph],
  )
  const topologyKey = projection.stages
    .map((stage) => `${stage.index}:${stage.nodes.map((node) => node.nodeId).join(',')}`)
    .join('|')
  const priorityStageKey = projection.stages
    .filter((stage) =>
      stage.nodes.some((node) => node.category === 'active' || node.category === 'attention'),
    )
    .map((stage) => stage.index)
    .join(',')
  const [expandedStages, setExpandedStages] = useState<Set<number>>(
    () => defaultExpandedStages(projection),
  )
  const [expandedNodeId, setExpandedNodeId] = useState<string>()
  const markerId = `coordinator-task-arrow-${useId().replaceAll(':', '')}`
  const canvasRef = useRef<HTMLDivElement>(null)
  const nodeElements = useRef(new Map<string, HTMLElement>())
  const [paths, setPaths] = useState<TaskFlowPath[]>([])

  useEffect(() => {
    setExpandedStages(defaultExpandedStages(projection))
    setExpandedNodeId(undefined)
  }, [topologyKey])

  useEffect(() => {
    const priorityStages = priorityStageKey
      .split(',')
      .filter(Boolean)
      .map(Number)
    if (priorityStages.length === 0) return
    setExpandedStages((current) => {
      const next = new Set(current)
      for (const stage of priorityStages) next.add(stage)
      return next
    })
  }, [priorityStageKey])

  const registerNodeElement = useCallback((nodeId: string, element: HTMLElement | null) => {
    if (element === null) nodeElements.current.delete(nodeId)
    else nodeElements.current.set(nodeId, element)
  }, [])

  const measurePaths = useCallback(() => {
    const canvas = canvasRef.current
    if (canvas === null) return
    const canvasBounds = canvas.getBoundingClientRect()
    const nextPaths = projection.edges.flatMap((edge): TaskFlowPath[] => {
      const source = nodeElements.current.get(edge.from)
      const target = nodeElements.current.get(edge.to)
      if (source === undefined || target === undefined) return []
      const sourceBounds = source.getBoundingClientRect()
      const targetBounds = target.getBoundingClientRect()
      const startX = sourceBounds.right - canvasBounds.left
      const startY = sourceBounds.top + sourceBounds.height / 2 - canvasBounds.top
      const endX = targetBounds.left - canvasBounds.left
      const endY = targetBounds.top + targetBounds.height / 2 - canvasBounds.top
      const bend = Math.max(24, (endX - startX) / 2)
      return [{
        key: `${edge.from}:${edge.to}`,
        data: `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`,
      }]
    })
    setPaths(nextPaths)
  }, [projection.edges])

  useLayoutEffect(() => {
    const frame = window.requestAnimationFrame(measurePaths)
    const observer = new ResizeObserver(measurePaths)
    if (canvasRef.current !== null) observer.observe(canvasRef.current)
    for (const element of new Set(nodeElements.current.values())) observer.observe(element)
    window.addEventListener('resize', measurePaths)
    return () => {
      window.cancelAnimationFrame(frame)
      observer.disconnect()
      window.removeEventListener('resize', measurePaths)
    }
  }, [collapsed, expandedNodeId, expandedStages, measurePaths, topologyKey])

  const toggleStage = (stage: CoordinatorTaskFlowStage) => {
    setExpandedStages((current) => {
      const next = new Set(current)
      if (next.has(stage.index)) {
        next.delete(stage.index)
        if (stage.nodes.some((node) => node.nodeId === expandedNodeId)) {
          setExpandedNodeId(undefined)
        }
      } else {
        next.add(stage.index)
      }
      return next
    })
  }

  const readOnly = session.archived_at !== null || session.goal?.status !== 'active'
  const approvals = session.autonomy.approvals.filter((approval) => approval.status === 'pending')
  const planStatus = planDisplayStatus(plan.status, projection)
  const canvasStyle = {
    minWidth: `${Math.max(680, projection.stages.length * 236)}px`,
    '--task-flow-stage-count': Math.max(1, projection.stages.length),
  } as CSSProperties

  return (
    <article className={'coordinator-task-flow-card ' + (collapsed ? 'collapsed' : '')}>
      <TaskFlowHeader
        title="当前任务流"
        description="状态变化会更新当前卡片；完整会话输出保留在委派详情中"
        status={planStatus}
        collapsed={collapsed}
        onToggleCollapsed={onToggleCollapsed}
      />
      {!collapsed && (
        <>
          <TaskFlowSummary projection={projection} />
          <div className="coordinator-task-flow-scroll">
            <div className="coordinator-task-flow-canvas" ref={canvasRef} style={canvasStyle}>
              <svg className="coordinator-task-flow-edges" aria-hidden="true">
                <defs>
                  <marker id={markerId} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                    <path d="M 0 0 L 8 4 L 0 8 z" />
                  </marker>
                </defs>
                {paths.map((path) => <path d={path.data} key={path.key} markerEnd={`url(#${markerId})`} />)}
              </svg>
              <div className="coordinator-task-flow-stages">
                {projection.stages.map((stage) => {
                  const expanded = expandedStages.has(stage.index)
                  return (
                    <section className={'coordinator-task-flow-stage ' + (expanded ? 'expanded' : 'collapsed')} key={stage.index}>
                      <button type="button" className="coordinator-task-flow-stage-header" onClick={() => toggleStage(stage)} aria-expanded={expanded}>
                        <span>阶段 {stage.index + 1}</span>
                        <small>{stage.nodes.length} 个任务</small>
                        <ChevronDown size={13} />
                      </button>
                      {expanded ? (
                        <div className="coordinator-task-flow-stage-nodes">
                          {stage.nodes.map((flowNode) => (
                            <CoordinatorTaskFlowNodeCard
                              flowNode={flowNode}
                              delegation={delegationFor(flowNode, delegationSnapshots)}
                              session={session}
                              readOnly={readOnly}
                              expanded={expandedNodeId === flowNode.nodeId}
                              onToggle={() => setExpandedNodeId((current) => current === flowNode.nodeId ? undefined : flowNode.nodeId)}
                              onOpenDelegation={onOpenDelegation}
                              onChanged={onChanged}
                              registerElement={registerNodeElement}
                              key={flowNode.nodeId}
                            />
                          ))}
                        </div>
                      ) : (
                        <button
                          type="button"
                          className="coordinator-task-flow-stage-summary"
                          onClick={() => toggleStage(stage)}
                          ref={(element) => {
                            for (const node of stage.nodes) registerNodeElement(node.nodeId, element)
                          }}
                        >
                          <StageSummary stage={stage} />
                        </button>
                      )}
                    </section>
                  )
                })}
              </div>
            </div>
          </div>
          {!readOnly && approvals.length > 0 && (
            <div className="coordinator-task-flow-approvals">
              <div className="coordinator-subtitle"><ShieldCheck size={14} />待处理审批</div>
              {approvals.map((approval) => (
                <ApprovalCard
                  approval={approval}
                  session={session}
                  onResolved={onChanged}
                  key={String(approval.approval_id)}
                />
              ))}
            </div>
          )}
          <details className="coordinator-debug coordinator-task-flow-debug">
            <summary>会话元数据</summary>
            <dl>
              <div><dt>session</dt><dd>{session.session_id}</dd></div>
              <div><dt>cognitive</dt><dd>{session.cognitive_session_id}</dd></div>
              <div><dt>revision</dt><dd>{session.revision}</dd></div>
              <div><dt>plan</dt><dd>{plan.plan_id} / rev {plan.revision}</dd></div>
            </dl>
          </details>
        </>
      )}
    </article>
  )
}

function TaskFlowHeader({
  title,
  description,
  status,
  collapsed = false,
  onToggleCollapsed,
}: {
  title: string
  description: string
  status?: string
  collapsed?: boolean
  onToggleCollapsed?: () => void
}) {
  return (
    <header className="coordinator-task-flow-header">
      <div className="coordinator-task-flow-heading">
        <span className="coordinator-task-flow-icon"><GitBranch size={15} /></span>
        <div><strong>{title}</strong><span>{description}</span></div>
      </div>
      {(status || onToggleCollapsed) && (
        <div className="coordinator-task-flow-header-actions">
          {status && <CoordinatorStatus status={status} />}
          {onToggleCollapsed && (
            <button type="button" className="coordinator-task-flow-collapse" onClick={onToggleCollapsed} aria-expanded={!collapsed} aria-label={collapsed ? '展开顶部当前任务流' : '折叠顶部当前任务流'}>
              <span>{collapsed ? '展开' : '折叠'}</span>
              <ChevronDown size={14} />
            </button>
          )}
        </div>
      )}
    </header>
  )
}

function TaskFlowSummary({ projection }: { projection: CoordinatorTaskFlowProjection }) {
  return (
    <div className="coordinator-task-flow-summary">
      <span><strong>{projection.summary.total}</strong>任务</span>
      <span className="active"><strong>{projection.summary.active}</strong>执行中</span>
      <span className="completed"><strong>{projection.summary.completed}</strong>已完成</span>
      <span className={projection.summary.attention > 0 ? 'attention' : ''}><strong>{projection.summary.attention}</strong>需处理</span>
    </div>
  )
}

function StageSummary({ stage }: { stage: CoordinatorTaskFlowStage }) {
  const active = stage.nodes.filter((node) => node.category === 'active').length
  const attention = stage.nodes.filter((node) => node.category === 'attention').length
  const completed = stage.nodes.filter((node) => node.category === 'completed').length
  return (
    <>
      <Layers3 size={16} />
      <strong>{stage.nodes.length} 个任务已折叠</strong>
      <span>
        {active > 0 && `${active} 执行中`}
        {active > 0 && (attention > 0 || completed > 0) && ' · '}
        {attention > 0 && `${attention} 需处理`}
        {attention > 0 && completed > 0 && ' · '}
        {completed > 0 && `${completed} 已完成`}
        {active === 0 && attention === 0 && completed === 0 && '点击展开任务'}
      </span>
    </>
  )
}

function ApprovalCard({
  approval,
  session,
  onResolved,
}: {
  approval: Record<string, unknown>
  session: CoordinatorSessionDomain
  onResolved: () => void
}) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string>()
  const resolve = async (approved: boolean) => {
    setPending(true)
    setError(undefined)
    try {
      await api.resolveCoordinatorApproval(session.session_id, String(approval.approval_id), {
        approved,
        actor_id: 'local-user',
        reason: approved ? '页面批准' : '页面拒绝',
        expected_session_revision: session.revision,
      })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      onResolved()
      setPending(false)
    }
  }
  return (
    <div className="coordinator-approval-card">
      <strong>{String(approval.reason ?? '需要人工审批')}</strong>
      <small>{String(approval.action_key ?? approval.approval_id)}</small>
      <div><button className="secondary-button" onClick={() => void resolve(false)} disabled={pending}>拒绝</button><button className="primary-button" onClick={() => void resolve(true)} disabled={pending}>批准</button></div>
      {error && <div className="coordinator-node-error">{error}</div>}
    </div>
  )
}

function defaultExpandedStages(projection: CoordinatorTaskFlowProjection): Set<number> {
  return new Set(
    projection.stages.filter((stage) => stage.defaultExpanded).map((stage) => stage.index),
  )
}

function delegationFor(
  node: CoordinatorTaskFlowNode,
  delegationSnapshots: Record<string, Delegation>,
): Delegation | undefined {
  const delegationId = node.node.execution?.delegation_id
  return delegationId === undefined ? undefined : delegationSnapshots[delegationId]
}

function planDisplayStatus(
  planStatus: string,
  projection: CoordinatorTaskFlowProjection,
): string {
  if (['completed', 'failed', 'cancelled'].includes(planStatus)) return planStatus
  if (projection.nodes.some((node) => node.status === 'reconciliation_required')) {
    return 'reconciliation_required'
  }
  if (projection.nodes.some((node) => node.status === 'review_required')) return 'review_required'
  if (projection.nodes.some((node) => node.status === 'failed')) return 'failed'
  return planStatus
}
