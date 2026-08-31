import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from 'react'
import { ChevronDown, ChevronRight, GitBranch, Layers3, ShieldCheck } from 'lucide-react'

import { api } from './api'
import { CoordinatorStatus } from './coordinator-status'
import {
  projectCoordinatorTaskFlow,
  type CoordinatorTaskFlowNode,
  type CoordinatorTaskFlowProjection,
  type CoordinatorTaskFlowStage,
} from './coordinator-task-flow'
import { MarkdownContent } from './MarkdownContent'
import type { CoordinatorPlan, CoordinatorSessionDomain, Delegation } from './types'

type CoordinatorTaskFlowCardProps = {
  session: CoordinatorSessionDomain
  delegationSnapshots: Record<string, Delegation>
  onOpenDelegation: (delegationId: string) => void
  onChanged: () => void
}

type TaskFlowPath = {
  key: string
  data: string
}

export function CoordinatorTaskFlowCard(props: CoordinatorTaskFlowCardProps) {
  if (props.session.plan === null) {
    return (
      <article className="coordinator-task-flow-card empty">
        <TaskFlowHeader title="当前任务流" description="Coordinator 尚未形成任务计划" />
        <div className="coordinator-task-flow-empty">
          <GitBranch size={20} />
          <strong>等待 Coordinator 拆解任务</strong>
          <span>计划形成后，依赖关系和执行状态会在这张卡片中持续更新。</span>
        </div>
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
  }, [expandedNodeId, expandedStages, measurePaths, topologyKey])

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
    <article className="coordinator-task-flow-card">
      <TaskFlowHeader
        title="当前任务流"
        description="状态变化会更新当前卡片；完整会话输出保留在委派详情中"
        status={planStatus}
      />
      <TaskFlowSummary projection={projection} />
      <div className="coordinator-task-flow-scroll">
        <div className="coordinator-task-flow-canvas" ref={canvasRef} style={canvasStyle}>
          <svg className="coordinator-task-flow-edges" aria-hidden="true">
            <defs>
              <marker id="coordinator-task-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M 0 0 L 8 4 L 0 8 z" />
              </marker>
            </defs>
            {paths.map((path) => <path d={path.data} key={path.key} />)}
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
                        <TaskFlowNodeCard
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
    </article>
  )
}

function TaskFlowHeader({
  title,
  description,
  status,
}: {
  title: string
  description: string
  status?: string
}) {
  return (
    <header className="coordinator-task-flow-header">
      <div className="coordinator-task-flow-heading">
        <span className="coordinator-task-flow-icon"><GitBranch size={15} /></span>
        <div><strong>{title}</strong><span>{description}</span></div>
      </div>
      {status && <CoordinatorStatus status={status} />}
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

function TaskFlowNodeCard({
  flowNode,
  delegation,
  session,
  readOnly,
  expanded,
  onToggle,
  onOpenDelegation,
  onChanged,
  registerElement,
}: {
  flowNode: CoordinatorTaskFlowNode
  delegation?: Delegation
  session: CoordinatorSessionDomain
  readOnly: boolean
  expanded: boolean
  onToggle: () => void
  onOpenDelegation: (delegationId: string) => void
  onChanged: () => void
  registerElement: (nodeId: string, element: HTMLElement | null) => void
}) {
  const node = flowNode.node
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string>()
  const [supplement, setSupplement] = useState('')
  const [reconcileOpen, setReconcileOpen] = useState(false)
  const [reconcileStatus, setReconcileStatus] = useState<'completed' | 'failed' | 'cancelled'>('failed')
  const [reconcileReason, setReconcileReason] = useState('')
  const executionStatus = delegation?.status ?? node.status
  const canAccept = !readOnly
    && node.status === 'review_required'
    && (delegation === undefined || delegation.status === 'completed')
  const canReconcile = !readOnly && (flowNode.status === 'reconciliation_required' || node.status === 'reconciliation_required')
  const canRetry = !readOnly && (['failed', 'review_required'].includes(flowNode.status) || ['failed', 'review_required'].includes(node.status))
  const canSupplement = !readOnly
    && ['active', 'awaiting_event', 'delegated', 'paused', 'waiting_input', 'completed'].includes(executionStatus)

  const run = async (operation: () => Promise<unknown>): Promise<boolean> => {
    setPending(true)
    setError(undefined)
    try {
      await operation()
      return true
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      return false
    } finally {
      onChanged()
      setPending(false)
    }
  }

  const reconcile = async () => {
    if (!delegation || !reconcileReason.trim()) {
      setError('请先选择可核实的委派版本并填写对账依据。')
      return
    }
    const succeeded = await run(() => api.coordinatorNodeReconcile(session.session_id, node.node_id, {
      expected_revision: delegation.revision,
      status: reconcileStatus,
      reason: reconcileReason.trim(),
      output: undefined,
    }))
    if (succeeded) {
      setReconcileOpen(false)
      setReconcileReason('')
    }
  }

  const sendSupplement = async () => {
    const message = supplement.trim()
    if (!message) return
    if (await run(() => api.coordinatorNodeContinue(session.session_id, node.node_id, message))) {
      setSupplement('')
    }
  }

  return (
    <article
      className={`coordinator-task-flow-node ${flowNode.category} ${expanded ? 'expanded' : ''}`}
    >
      <button
        type="button"
        className="coordinator-task-flow-node-toggle"
        onClick={onToggle}
        aria-expanded={expanded}
        ref={(element) => registerElement(flowNode.nodeId, element)}
      >
        <span className="coordinator-task-flow-node-title">
          <CoordinatorStatus status={flowNode.status} />
          <strong>{node.intent.objective}</strong>
        </span>
        <span className="coordinator-task-flow-node-chevron"><ChevronDown size={14} /></span>
      </button>
      <div className="coordinator-task-flow-node-meta">
        <span>{node.node_id}</span>
        <span>attempt {node.attempt}</span>
        {node.selection && <span>{node.selection.provider_id} / {node.selection.model_id}{node.selection.effort ? ` · ${node.selection.effort}` : ''}</span>}
      </div>
      {expanded && (
        <div className="coordinator-task-flow-node-details">
          {flowNode.dependencies.length > 0 && (
            <div className="coordinator-task-flow-dependencies">
              <span>依赖</span>
              {flowNode.dependencies.map((dependency) => <code key={dependency}>{dependency}</code>)}
            </div>
          )}
          {node.intent.acceptance_criteria.length > 0 && (
            <div className="coordinator-task-flow-detail-section">
              <strong>验收标准</strong>
              <ul>{node.intent.acceptance_criteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul>
            </div>
          )}
          {node.execution && (
            <div className="coordinator-task-flow-delegation">
              <div>
                <span>委派会话</span>
                <code>{node.execution.delegation_id}</code>
                {flowNode.childDelegationCount > 0 && <small>{flowNode.childDelegationCount} 个子委派</small>}
              </div>
              <button type="button" className="secondary-button" onClick={() => onOpenDelegation(node.execution!.delegation_id)}>打开完整委派 / TUI <ChevronRight size={12} /></button>
            </div>
          )}
          {delegation?.report?.error_code && <div className="coordinator-node-error-code">{delegation.report.error_code}</div>}
          {delegation?.report?.error_message && <div className="coordinator-node-error">{delegation.report.error_message}</div>}
          {delegation?.report?.output != null && <TaskResultSummary output={delegation.report.output} />}
          {(canAccept || canReconcile || canRetry) && (
            <div className="coordinator-node-actions">
              {canReconcile && <button type="button" className="warning-button" onClick={() => setReconcileOpen((value) => !value)} disabled={pending}>对账</button>}
              {canAccept && <button type="button" className="primary-button" onClick={() => void run(() => api.coordinatorNodeAccept(session.session_id, node.node_id, session.revision))} disabled={pending}>验收通过</button>}
              {canRetry && <button type="button" className="secondary-button" onClick={() => void run(() => api.coordinatorNodeRetry(session.session_id, node.node_id))} disabled={pending}>重试</button>}
            </div>
          )}
          {canSupplement && (
            <div className="coordinator-node-supplement">
              <textarea value={supplement} onChange={(event) => setSupplement(event.target.value)} placeholder="要求 Agent 补充说明或证据…" rows={2} disabled={pending} />
              <button type="button" className="secondary-button" onClick={() => void sendSupplement()} disabled={pending || !supplement.trim()}>要求补充</button>
            </div>
          )}
          {reconcileOpen && (
            <div className="coordinator-reconcile-form">
              <label>对账结论<select value={reconcileStatus} onChange={(event) => setReconcileStatus(event.target.value as typeof reconcileStatus)}><option value="failed">确认失败</option><option value="completed">确认已完成</option><option value="cancelled">确认已取消</option></select></label>
              <label>对账依据<textarea value={reconcileReason} onChange={(event) => setReconcileReason(event.target.value)} placeholder="例如：Agent 会话没有输出，无法证明任务成功。" rows={3} disabled={pending} /></label>
              <div className="coordinator-node-actions"><button type="button" className="secondary-button" onClick={() => setReconcileOpen(false)} disabled={pending}>取消</button><button type="button" className="warning-button" onClick={() => void reconcile()} disabled={pending || !reconcileReason.trim()}>提交对账</button></div>
            </div>
          )}
          {error && <div className="coordinator-node-error">{error}</div>}
        </div>
      )}
    </article>
  )
}

function TaskResultSummary({ output }: { output: unknown }) {
  const summary = summarizeOutput(output)
  return (
    <div className="coordinator-task-flow-result">
      <strong>结果摘要</strong>
      {summary.markdown ? <MarkdownContent content={summary.content} /> : <pre>{summary.content}</pre>}
      {summary.truncated && <small>内容已截断，请打开完整委派查看全部结果。</small>}
    </div>
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

function summarizeOutput(output: unknown): {
  content: string
  markdown: boolean
  truncated: boolean
} {
  const markdown = typeof output === 'string'
  let content: string
  if (markdown) {
    content = output.trim()
  } else {
    try {
      content = JSON.stringify(output, null, 2) ?? String(output)
    } catch {
      content = String(output)
    }
  }
  const limit = 1_600
  return {
    content: content.length > limit ? content.slice(0, limit).trimEnd() + '…' : content,
    markdown,
    truncated: content.length > limit,
  }
}
