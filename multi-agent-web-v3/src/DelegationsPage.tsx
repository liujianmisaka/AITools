import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Bot,
  BrainCircuit,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Clock3,
  FileCode2,
  GitBranch,
  ListChecks,
  LoaderCircle,
  MessageSquareText,
  RefreshCw,
  Terminal,
  Wrench,
  X,
} from 'lucide-react'
import { delegationActor } from './api'
import { useDelegationEvents, type DelegationConnectionState } from './useDelegationEvents'
import {
  useDelegationSession,
  type DelegationSessionConnectionState,
} from './useDelegationSession'
import type { AgentSessionItem, AgentSessionTurn } from './sessionTimeline'
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
  const applySnapshot = (snapshot: Delegation) => {
    setLiveDelegation(snapshot)
    onSnapshot?.(snapshot)
  }
  const interactionLive = useDelegationEvents(delegation.delegation_id, applySnapshot)
  const sessionLive = useDelegationSession(delegation.delegation_id, applySnapshot)
  const session = sessionLive.session
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
          <LiveConnection
            state={sessionLive.connection}
            lastSequence={sessionLive.lastSequence}
            label="Agent 会话"
          />
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
            <dd>{session?.delegation.session_id ?? liveDelegation.session_id ?? '—'}</dd>
          </div>
          <div>
            <dt>Provider</dt>
            <dd>{session?.provider_id ?? '—'}</dd>
          </div>
          <div>
            <dt>模型</dt>
            <dd>
              {session?.model ?? '—'} · {session?.effort ?? '—'}
            </dd>
          </div>
          <div>
            <dt>Agent 会话</dt>
            <dd>{session?.provider_session_id ?? '等待绑定'}</dd>
          </div>
          <div>
            <dt>Agent 操作</dt>
            <dd>{session?.provider_operation_id ?? '—'}</dd>
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
        {sessionLive.error && <div className="warning-banner">Agent 会话：{sessionLive.error}</div>}
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
        <DelegationSessionConsole
          timeline={sessionLive.timeline}
          lastSequence={sessionLive.lastSequence}
          terminalOutput={sessionLive.terminalOutput}
          stage={session?.stage ?? liveDelegation.status}
        />
        <details className="delegation-debug">
          <summary>
            交互消息调试流 · {interactionLive.connection} · {interactionLive.lastSequence} 条
          </summary>
          <DelegationTimeline messages={interactionLive.messages} />
        </details>
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
  label = '实时连接',
}: {
  state: DelegationConnectionState | DelegationSessionConnectionState
  lastSequence: number
  label?: string
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
      {label}：{labels[state]} · 事件 {lastSequence}
    </span>
  )
}

function DelegationSessionConsole({
  timeline,
  lastSequence,
  terminalOutput,
  stage,
}: {
  timeline: AgentSessionTurn[]
  lastSequence: number
  terminalOutput: unknown
  stage: string
}) {
  const transcriptRef = useRef<HTMLDivElement>(null)
  const followOutput = useRef(true)

  useEffect(() => {
    if (!followOutput.current || transcriptRef.current === null) return
    transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight
  }, [lastSequence])

  const handleTranscriptScroll = () => {
    const transcript = transcriptRef.current
    if (transcript === null) return
    followOutput.current =
      transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 48
  }

  return (
    <section className="agent-session-console">
      <div className="session-console-header">
        <div>
          <div className="result-title">真实 Agent 会话</div>
          <strong>{sessionStageLabel(stage)}</strong>
        </div>
        <span className="session-event-count">事件 #{lastSequence}</span>
      </div>
      <div
        className="agent-session-transcript"
        ref={transcriptRef}
        onScroll={handleTranscriptScroll}
      >
        {timeline.length === 0 ? (
          <div className="timeline-empty">会话已建立，等待 Agent 产生实时事件…</div>
        ) : (
          timeline.map((turn) => <AgentSessionTurnCard key={turn.key} turn={turn} />)
        )}
      </div>
      {terminalOutput !== null &&
        terminalOutput !== undefined &&
        typeof terminalOutput !== 'string' && (
          <details className="session-terminal-output">
            <summary>结构化终态输出</summary>
            <pre>{formatEventPayload(terminalOutput)}</pre>
          </details>
        )}
    </section>
  )
}

function AgentSessionTurnCard({ turn }: { turn: AgentSessionTurn }) {
  return (
    <section className={'agent-session-turn ' + (turn.completedAt ? 'completed' : 'active')}>
      <div className="agent-session-turn-header">
        <div>
          <span>激活 {turn.activationNumber ?? '—'}</span>
          <strong>{turnStatusLabel(turn.status)}</strong>
        </div>
        <small>
          {compactId(turn.turnId)} · #{turn.firstSequence}–{turn.lastSequence}
        </small>
      </div>
      <div className="agent-session-items">
        {turn.items.length === 0 ? (
          <div className="agent-session-waiting">Agent 已开始，等待工作项…</div>
        ) : (
          turn.items.map((item) => <AgentSessionItemCard key={item.key} item={item} />)
        )}
      </div>
    </section>
  )
}

function AgentSessionItemCard({ item }: { item: AgentSessionItem }) {
  return (
    <article className={`agent-session-item ${item.kind} ${item.completed ? 'completed' : 'live'}`}>
      <div className="agent-session-item-rail">
        <AgentSessionItemIcon kind={item.kind} />
      </div>
      <div className="agent-session-item-main">
        <div className="agent-session-item-header">
          <div>
            <strong>{sessionItemLabel(item)}</strong>
            <span>{itemStatusLabel(item)}</span>
          </div>
          <small>#{item.lastSequence} · {formatEventTime(item.updatedAt)}</small>
        </div>
        {item.command && <code className="agent-session-command">{item.command}</code>}
        {item.kind === 'plan' && item.plan.length > 0 && (
          <ol className="agent-session-plan">
            {item.plan.map((entry, index) => (
              <li className={entry.status ?? 'pending'} key={`${entry.step}-${index}`}>
                <CircleCheck size={12} />
                <span>{entry.step}</span>
              </li>
            ))}
          </ol>
        )}
        {item.kind === 'file' && item.changes.length > 0 && (
          <ul className="agent-session-files">
            {item.changes.map((change) => (
              <li key={`${change.kind ?? 'change'}:${change.path}`}>
                <span>{change.kind ?? 'change'}</span>
                <code>{change.path}</code>
              </li>
            ))}
          </ul>
        )}
        {item.text && <pre className="agent-session-item-output">{item.text}</pre>}
      </div>
    </article>
  )
}

function AgentSessionItemIcon({ kind }: { kind: AgentSessionItem['kind'] }) {
  if (kind === 'message') return <MessageSquareText size={14} />
  if (kind === 'reasoning') return <BrainCircuit size={14} />
  if (kind === 'plan') return <ListChecks size={14} />
  if (kind === 'tool') return <Wrench size={14} />
  if (kind === 'command') return <Terminal size={14} />
  if (kind === 'file') return <FileCode2 size={14} />
  if (kind === 'task') return <Bot size={14} />
  return <CircleAlert size={14} />
}

function sessionItemLabel(item: AgentSessionItem): string {
  if (item.kind === 'message') return item.parentItemId ? '子 Agent 消息' : 'Agent 消息'
  if (item.kind === 'reasoning') return '推理摘要'
  if (item.kind === 'plan') return '执行计划'
  if (item.kind === 'tool') return item.name ? `工具 · ${item.name}` : '工具调用'
  if (item.kind === 'command') return '命令执行'
  if (item.kind === 'file') return '文件变更'
  if (item.kind === 'task') return item.name ? `子任务 · ${item.name}` : '子任务'
  return '状态异常'
}

function itemStatusLabel(item: AgentSessionItem): string {
  if (item.status) return turnStatusLabel(item.status)
  return item.completed ? '已完成' : '实时输出中'
}

function turnStatusLabel(status?: string): string {
  if (!status) return '执行中'
  const labels: Record<string, string> = {
    in_progress: '执行中',
    running: '执行中',
    succeeded: '已完成',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    interrupted: '已中断',
    stopping: '停止中',
    reconciliation_required: '需要人工对账',
  }
  return labels[status] ?? status
}

function compactId(value: string): string {
  return value.length <= 18 ? value : `${value.slice(0, 8)}…${value.slice(-6)}`
}

function sessionStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    created: '已创建',
    admission: '准入检查',
    activation_started: 'Agent 执行中',
    terminal: '本次激活已结束',
    active: 'Agent 执行中',
    running: 'Agent 执行中',
    completed: '已完成',
    failed: '执行失败',
    cancelled: '已取消',
    session_closed: '会话已关闭',
    reconciliation_required: '需要人工对账',
  }
  return labels[stage] ?? stage
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

function formatEventPayload(payload: unknown): string {
  try {
    return JSON.stringify(payload, null, 2) ?? String(payload)
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
