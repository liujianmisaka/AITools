import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Clock3,
  GitBranch,
  LoaderCircle,
  RefreshCw,
  X,
} from 'lucide-react'
import { delegationActor } from './api'
import { useDelegationEvents, type DelegationConnectionState } from './useDelegationEvents'
import type { Delegation, InteractionMessage } from './types'

const delegationStatusLabels: Record<string, string> = {
  proposed: '待准入',
  admitted: '已准入',
  preparing: '准备中',
  active: '执行中',
  paused: '已暂停',
  waiting_input: '等待输入',
  reporting: '汇报中',
  completed: '已完成',
  rejected: '已拒绝',
  failed: '失败',
  cancelled: '已取消',
  reconciliation_required: '需人工对账',
  reconciling: '对账中',
}

type DelegationsPageProps = {
  delegations: Delegation[]
  loading: boolean
  error?: string
  onRefresh: () => void
  onSelect: (delegationId: string) => void
}

export function DelegationsPage({
  delegations,
  loading,
  error,
  onRefresh,
  onSelect,
}: DelegationsPageProps) {
  const [statusFilter, setStatusFilter] = useState('all')
  const activeCount = delegations.filter((delegation) =>
    ['active', 'preparing', 'reporting', 'reconciling'].includes(delegation.status),
  ).length
  const waitingCount = delegations.filter((delegation) =>
    ['proposed', 'admitted', 'paused', 'waiting_input'].includes(delegation.status),
  ).length
  const reconciliationCount = delegations.filter(
    (delegation) => delegation.status === 'reconciliation_required',
  ).length
  const statuses = useMemo(
    () => Array.from(new Set(delegations.map((delegation) => delegation.status))).sort(),
    [delegations],
  )
  const visibleDelegations =
    statusFilter === 'all'
      ? delegations
      : delegations.filter((delegation) => delegation.status === statusFilter)

  return (
    <>
      <section className="metric-grid delegation-metrics">
        <DelegationMetric
          icon={<GitBranch size={18} />}
          label="可见委派"
          value={delegations.length}
        />
        <DelegationMetric
          icon={<LoaderCircle size={18} />}
          label="进行中"
          value={activeCount}
          tone="blue"
        />
        <DelegationMetric
          icon={<Clock3 size={18} />}
          label="等待处理"
          value={waitingCount}
        />
        <DelegationMetric
          icon={<CircleAlert size={18} />}
          label="需人工对账"
          value={reconciliationCount}
          tone="red"
        />
      </section>

      <section className="panel delegation-panel">
        <div className="panel-header">
          <div>
            <h2>委派任务</h2>
            <p>
              观察主体 {delegationActor.actorId} / {delegationActor.actorKind}；详情优先使用事件流，
              断线时自动重连并以状态接口兜底。
            </p>
          </div>
          <div className="panel-tools">
            <label className="compact-select">
              状态
              <select
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value)}
              >
                <option value="all">全部</option>
                {statuses.map((status) => (
                  <option value={status} key={status}>
                    {delegationStatusLabels[status] ?? status}
                  </option>
                ))}
              </select>
            </label>
            <button className="icon-button" onClick={onRefresh} title="刷新">
              <RefreshCw size={16} />
            </button>
          </div>
        </div>

        {error && <div className="error-banner">委派列表读取失败：{error}</div>}
        {loading ? (
          <DelegationEmptyState
            icon={<LoaderCircle className="spin" />}
            title="正在加载委派"
          />
        ) : delegations.length === 0 ? (
          <DelegationEmptyState
            icon={<GitBranch />}
            title="还没有可见委派"
            description="通过 MCP 或 Control Plane 创建一个委派任务。"
          />
        ) : visibleDelegations.length === 0 ? (
          <DelegationEmptyState icon={<GitBranch />} title="没有匹配状态的委派" />
        ) : (
          <div className="delegation-table">
            <div className="delegation-table-head">
              <span>委派</span>
              <span>当前调用</span>
              <span>会话</span>
              <span>层级</span>
              <span />
            </div>
            {visibleDelegations.map((delegation) => (
              <DelegationRow
                key={delegation.delegation_id}
                delegation={delegation}
                onClick={() => onSelect(delegation.delegation_id)}
              />
            ))}
          </div>
        )}
      </section>
    </>
  )
}

export function DelegationDrawer({
  delegation,
  onClose,
  onSnapshot,
}: {
  delegation: Delegation
  onClose: () => void
  onSnapshot?: (snapshot: Delegation) => void
}) {
  const [liveDelegation, setLiveDelegation] = useState(delegation)
  useEffect(() => setLiveDelegation(delegation), [delegation])
  const live = useDelegationEvents(delegation.delegation_id, (snapshot) => {
    setLiveDelegation(snapshot)
    onSnapshot?.(snapshot)
  })
  const reportText = useMemo(
    () =>
      liveDelegation.report?.output === undefined || liveDelegation.report?.output === null
        ? ''
        : JSON.stringify(liveDelegation.report.output, null, 2),
    [liveDelegation.report?.output],
  )

  return (
    <div
      className="drawer-backdrop"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <aside className="drawer delegation-drawer">
        <div className="drawer-header">
          <div>
            <span className="eyebrow">DELEGATION SNAPSHOT</span>
            <h2>{liveDelegation.delegation_id}</h2>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭">
            <X size={18} />
          </button>
        </div>
        <div className="drawer-status">
          <DelegationStatus status={liveDelegation.status} />
          <span className="muted">版本 {liveDelegation.revision}</span>
          <LiveConnection state={live.connection} lastSequence={live.lastSequence} />
        </div>
        <dl className="detail-list">
          <div>
            <dt>当前调用</dt>
            <dd>{currentInvocationLabel(liveDelegation)}</dd>
          </div>
          <div>
            <dt>当前激活</dt>
            <dd>{liveDelegation.current_activation_id ?? '—'}</dd>
          </div>
          <div>
            <dt>会话</dt>
            <dd>{liveDelegation.session_id ?? '—'}</dd>
          </div>
          <div>
            <dt>父委派</dt>
            <dd>{liveDelegation.parent_delegation_id ?? '—'}</dd>
          </div>
          <div>
            <dt>层级</dt>
            <dd>
              深度 {liveDelegation.depth} · 激活 {liveDelegation.activation_count} · 子委派{' '}
              {liveDelegation.child_delegation_ids.length}
            </dd>
          </div>
        </dl>
        {live.error && <div className="warning-banner">实时事件：{live.error}</div>}
        {liveDelegation.status === 'reconciliation_required' && (
          <div className="warning-banner">
            <strong>需要人工对账</strong>
            <span>系统无法证明外部 Agent 是否已启动，不会自动重复执行。</span>
          </div>
        )}
        {liveDelegation.report?.error_message && (
          <div className="error-banner">
            <strong>{liveDelegation.report.error_code ?? '委派错误'}</strong>
            <span>{liveDelegation.report.error_message}</span>
          </div>
        )}
        <DelegationTimeline messages={live.messages} />
        {reportText && (
          <div className="result-block">
            <div className="result-title">最近报告</div>
            <pre>{reportText}</pre>
          </div>
        )}
        <div className="drawer-actions">
          <button className="secondary-button" onClick={onClose}>
            关闭
          </button>
        </div>
      </aside>
    </div>
  )
}

function LiveConnection({
  state,
  lastSequence,
}: {
  state: DelegationConnectionState
  lastSequence: number
}) {
  const labels: Record<DelegationConnectionState, string> = {
    connecting: '连接中',
    connected: '实时连接',
    reconnecting: '重连中',
    ended: '流已结束',
  }
  return (
    <span className={'live-connection ' + state}>
      <span className="live-connection-dot" />
      {labels[state]} · 事件 {lastSequence}
    </span>
  )
}

function DelegationTimeline({ messages }: { messages: InteractionMessage[] }) {
  return (
    <section className="delegation-timeline-block">
      <div className="result-title">委派会话事件</div>
      {messages.length === 0 ? (
        <div className="timeline-empty">等待 Agent 发布第一条事件…</div>
      ) : (
        <ol className="delegation-timeline">
          {messages.map((message) => (
            <li key={message.message_id} className="delegation-timeline-item">
              <div className="timeline-item-head">
                <span className={'timeline-type ' + message.message_type}>
                  {message.message_type}
                </span>
                <span className="muted">#{message.sequence} · {formatEventTime(message.created_at)}</span>
              </div>
              <div className="timeline-item-meta">
                {message.sender.display_name || message.sender.principal_id} · {message.delivery_status}
              </div>
              <pre>{formatEventPayload(message.payload)}</pre>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function formatEventPayload(payload: Record<string, unknown>): string {
  try {
    return JSON.stringify(payload, null, 2)
  } catch {
    return String(payload)
  }
}

function formatEventTime(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString()
}

function DelegationRow({
  delegation,
  onClick,
}: {
  delegation: Delegation
  onClick: () => void
}) {
  return (
    <button className="delegation-row" onClick={onClick}>
      <div className="delegation-name">
        <DelegationStatus status={delegation.status} />
        <strong>{delegation.delegation_id}</strong>
        <small>
          revision {delegation.revision} · activation {delegation.activation_count}
        </small>
      </div>
      <span className="muted">{currentInvocationLabel(delegation)}</span>
      <span className="muted">{delegation.session_id ?? '无会话'}</span>
      <span className="muted">
        深度 {delegation.depth} · 子委派 {delegation.child_delegation_ids.length}
      </span>
      <ChevronRight size={16} className="row-arrow" />
    </button>
  )
}

function DelegationStatus({ status }: { status: string }) {
  return (
    <span className={'status-badge delegation-status ' + status}>
      <DelegationStatusIcon status={status} />
      {delegationStatusLabels[status] ?? status}
    </span>
  )
}

function DelegationStatusIcon({ status }: { status: string }) {
  if (status === 'completed') return <CircleCheck size={14} />
  if (['failed', 'rejected', 'reconciliation_required'].includes(status)) {
    return <CircleAlert size={14} />
  }
  if (['active', 'preparing', 'reporting', 'reconciling'].includes(status)) {
    return <LoaderCircle size={14} className="spin" />
  }
  return <Clock3 size={14} />
}

function currentInvocationLabel(delegation: Delegation) {
  if (delegation.current_invocation_id) return delegation.current_invocation_id
  return delegation.activation_count > 0 ? '当前无活动调用' : '尚未激活'
}

function DelegationMetric({
  icon,
  label,
  value,
  tone = '',
}: {
  icon: ReactNode
  label: string
  value: number
  tone?: string
}) {
  return (
    <div className={'metric-card ' + tone}>
      <div className="metric-icon">{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
    </div>
  )
}

function DelegationEmptyState({
  icon,
  title,
  description,
}: {
  icon: ReactNode
  title: string
  description?: string
}) {
  return (
    <div className="empty-state">
      <div>{icon}</div>
      <strong>{title}</strong>
      {description && <p>{description}</p>}
    </div>
  )
}
