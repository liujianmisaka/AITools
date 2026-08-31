import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

import { api } from './api'
import { CoordinatorStatus } from './coordinator-status'
import {
  coordinatorTaskActionAvailability,
  type CoordinatorTaskFlowNode,
} from './coordinator-task-flow'
import { MarkdownContent } from './MarkdownContent'
import type { CoordinatorSessionDomain, Delegation } from './types'

export function CoordinatorTaskFlowNodeCard({
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
  const actions = coordinatorTaskActionAvailability(
    node.status,
    delegation?.status,
    readOnly,
  )

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
    <article className={`coordinator-task-flow-node ${flowNode.category} ${expanded ? 'expanded' : ''}`}>
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
          {(actions.accept || actions.reconcile || actions.retry) && (
            <div className="coordinator-node-actions">
              {actions.reconcile && <button type="button" className="warning-button" onClick={() => setReconcileOpen((value) => !value)} disabled={pending}>对账</button>}
              {actions.accept && <button type="button" className="primary-button" onClick={() => void run(() => api.coordinatorNodeAccept(session.session_id, node.node_id, session.revision))} disabled={pending}>验收通过</button>}
              {actions.retry && <button type="button" className="secondary-button" onClick={() => void run(() => api.coordinatorNodeRetry(session.session_id, node.node_id))} disabled={pending}>重试</button>}
            </div>
          )}
          {actions.supplement && (
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
