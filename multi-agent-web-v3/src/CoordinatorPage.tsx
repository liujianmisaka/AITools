import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  BrainCircuit,
  CircleAlert,
  CircleCheck,
  Clock3,
  GitBranch,
  LoaderCircle,
  MessageSquareText,
  Plus,
  RefreshCw,
  Send,
  ShieldCheck,
  Square,
  X,
} from 'lucide-react'
import { api, coordinatorStreamUrl } from './api'
import { MarkdownContent } from './MarkdownContent'
import type {
  CoordinatorEvent,
  CoordinatorPlanNode,
  CoordinatorSession,
  CoordinatorSessionDomain,
  CoordinatorSessionSummary,
} from './types'

const statusLabels: Record<string, string> = {
  active: '执行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  draft: '计划草稿',
  ready: '待派遣',
  running: '执行中',
  waiting: '等待事件',
  reviewing: '待验收',
}

type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'ended'

export function CoordinatorPage() {
  const [sessions, setSessions] = useState<CoordinatorSessionSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [record, setRecord] = useState<CoordinatorSession | null>(null)
  const [events, setEvents] = useState<CoordinatorEvent[]>([])
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [error, setError] = useState<string>()
  const [loading, setLoading] = useState(true)
  const [composerOpen, setComposerOpen] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)

  const refreshSessions = useCallback(async () => {
    try {
      const next = await api.coordinatorSessions()
      setSessions(next)
      setSelectedId((current) => {
        if (current !== null && next.some((session) => session.session_id === current)) return current
        return next[0]?.session_id ?? null
      })
      setError(undefined)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshSessions()
  }, [refreshSessions, refreshToken])

  useEffect(() => {
    if (selectedId === null) {
      setRecord(null)
      setEvents([])
      setConnection('ended')
      return
    }
    let disposed = false
    let source: EventSource | null = null
    let reconnectTimer: number | undefined
    let lastSequence = 0
    let currentEvents: CoordinatorEvent[] = []
    let connectionState: ConnectionState = 'connecting'

    const updateConnection = (next: ConnectionState) => {
      connectionState = next
      if (!disposed) setConnection(next)
    }

    const mergeEvents = (incoming: CoordinatorEvent[]) => {
      const bySequence = new Map<number, CoordinatorEvent>()
      for (const event of [...currentEvents, ...incoming]) bySequence.set(event.sequence, event)
      currentEvents = [...bySequence.values()].sort((left, right) => left.sequence - right.sequence).slice(-800)
      lastSequence = currentEvents.reduce((highest, event) => Math.max(highest, event.sequence), lastSequence)
      if (!disposed) setEvents(currentEvents)
    }

    const refreshRecord = async (includeEvents: boolean) => {
      try {
        const [nextRecord, nextEvents] = await Promise.all([
          api.coordinatorSession(selectedId),
          includeEvents ? api.coordinatorEvents(selectedId, lastSequence + 1) : Promise.resolve([]),
        ])
        if (disposed) return
        setRecord(nextRecord)
        if (nextEvents.length > 0) mergeEvents(nextEvents)
        setError(undefined)
      } catch (reason) {
        if (!disposed) setError(reason instanceof Error ? reason.message : String(reason))
      }
    }

    const openStream = (nextSequence: number) => {
      if (disposed) return
      source?.close()
      updateConnection('connecting')
      const nextSource = new EventSource(coordinatorStreamUrl(selectedId, nextSequence))
      source = nextSource
      nextSource.onopen = () => {
        if (!disposed) {
          updateConnection('connected')
          setError(undefined)
        }
      }
      nextSource.onerror = () => {
        if (!disposed) {
          updateConnection('reconnecting')
          if (nextSource.readyState === EventSource.CLOSED && reconnectTimer === undefined) {
            reconnectTimer = window.setTimeout(() => {
              reconnectTimer = undefined
              openStream(lastSequence + 1)
            }, 3_000)
          }
        }
      }
      nextSource.addEventListener('coordinator.session.event', (event) => {
        try {
          const next = JSON.parse((event as MessageEvent<string>).data) as CoordinatorEvent
          mergeEvents([next])
          void refreshRecord(false)
        } catch (reason) {
          if (!disposed) setError(reason instanceof Error ? reason.message : 'Coordinator 事件格式无效')
        }
      })
      nextSource.addEventListener('coordinator.session.end', () => {
        if (!disposed) {
          updateConnection('ended')
          nextSource.close()
          void refreshRecord(true)
        }
      })
    }

    const initialize = async () => {
      await refreshRecord(true)
      if (!disposed) openStream(lastSequence + 1)
    }
    void initialize()
    const fallbackTimer = window.setInterval(() => {
      if (!disposed) void refreshRecord(connectionState !== 'connected')
    }, 5_000)
    return () => {
      disposed = true
      source?.close()
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer)
      window.clearInterval(fallbackTimer)
    }
  }, [selectedId, refreshToken])

  const selectedSummary = useMemo(() => sessions.find((session) => session.session_id === selectedId) ?? null, [selectedId, sessions])

  return (
    <div className="coordinator-page">
      <section className="panel coordinator-pane coordinator-sessions-pane">
        <div className="panel-header coordinator-pane-header">
          <div><h2>Coordinator 会话</h2><p>{sessions.length} 个持久会话</p></div>
          <div className="panel-tools"><button className="icon-button" onClick={() => setRefreshToken((value) => value + 1)} title="刷新"><RefreshCw size={16} /></button><button className="icon-button" onClick={() => setComposerOpen(true)} title="新建会话"><Plus size={16} /></button></div>
        </div>
        {error && <div className="error-banner coordinator-error">读取 Coordinator 失败：{error}</div>}
        {loading ? <CoordinatorEmpty icon={<LoaderCircle className="spin" />} title="正在加载会话" /> : sessions.length === 0 ? <CoordinatorEmpty icon={<BrainCircuit />} title="还没有 Coordinator 会话" description="创建会话，让 Coordinator 持续理解目标并调度委派。" /> : <div className="coordinator-session-list">{sessions.map((session) => <button className={'coordinator-session-row ' + (session.session_id === selectedId ? 'selected' : '')} key={session.session_id} onClick={() => setSelectedId(session.session_id)}><div className="coordinator-session-row-head"><CoordinatorStatus status={session.plan_status ?? session.goal?.status ?? 'active'} /><span>rev {session.revision}</span></div><strong>{session.session_id}</strong><small>{session.goal?.objective ?? '未设置目标'}</small></button>)}</div>}
      </section>

      <section className="panel coordinator-pane coordinator-conversation-pane">
        <div className="panel-header coordinator-pane-header"><div><span className="eyebrow">CONTINUOUS COORDINATION</span><h2>{selectedSummary?.goal?.objective ?? selectedId ?? '选择一个会话'}</h2></div><div className={'coordinator-connection ' + connection}><span />{connectionLabel(connection)}</div></div>
        {record === null ? <CoordinatorEmpty icon={<MessageSquareText />} title="选择一个会话查看对话" /> : <CoordinatorConversation events={events} session={record.session} onMessage={async (message) => { await api.sendCoordinatorMessage(record.session.session_id, message); setRefreshToken((value) => value + 1) }} onCancel={async () => { await api.cancelCoordinatorSession(record.session.session_id, '用户从 Coordinator 页面取消目标'); setRefreshToken((value) => value + 1) }} />}
      </section>

      <section className="panel coordinator-pane coordinator-plan-pane">
        <div className="panel-header coordinator-pane-header"><div><h2>当前计划</h2><p>{record?.session.plan ? 'revision ' + record.session.plan.revision : '等待 Coordinator 形成计划'}</p></div><GitBranch size={18} className="coordinator-panel-icon" /></div>
        {record === null ? <CoordinatorEmpty icon={<GitBranch />} title="暂无计划" /> : <CoordinatorPlan session={record.session} onApprovalResolved={() => setRefreshToken((value) => value + 1)} />}
      </section>
      {composerOpen && <CoordinatorComposer onClose={() => setComposerOpen(false)} onCreated={(sessionId) => { setComposerOpen(false); setSelectedId(sessionId); setRefreshToken((value) => value + 1) }} />}
    </div>
  )
}

function CoordinatorConversation({ events, session, onMessage, onCancel }: { events: CoordinatorEvent[]; session: CoordinatorSessionDomain; onMessage: (message: string) => Promise<void>; onCancel: () => Promise<void> }) {
  const [message, setMessage] = useState('')
  const [pending, setPending] = useState(false)
  const transcriptRef = useRef<HTMLDivElement>(null)
  useEffect(() => { const element = transcriptRef.current; if (element !== null) element.scrollTop = element.scrollHeight }, [events.length])
  const submit = async (event: FormEvent) => { event.preventDefault(); const next = message.trim(); if (!next || pending) return; setPending(true); try { await onMessage(next); setMessage('') } finally { setPending(false) } }
  const canCancel = session.goal?.status === 'active'
  return <div className="coordinator-conversation-content"><div className="coordinator-transcript" ref={transcriptRef}>{events.length === 0 ? <CoordinatorEmpty icon={<Clock3 />} title="等待事件" description="Coordinator 激活后，用户消息、计划决策和委派状态会显示在这里。" /> : events.map((event) => <CoordinatorEventCard event={event} key={event.event_id} />)}</div><form className="coordinator-composer" onSubmit={submit}><textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="继续补充目标、回答 Coordinator 的问题，或要求重新规划…" disabled={pending} rows={3} /><div className="coordinator-composer-actions"><span>当前目标：{session.goal?.status ?? '未开始'} · revision {session.revision}</span><div>{canCancel && <button type="button" className="danger-button" onClick={() => { if (!pending) { setPending(true); void onCancel().finally(() => setPending(false)) } }} disabled={pending}><Square size={14} />取消目标</button>}<button type="submit" className="primary-button" disabled={pending || message.trim().length === 0}><Send size={14} />发送</button></div></div></form></div>
}

function CoordinatorEventCard({ event }: { event: CoordinatorEvent }) {
  const payload = event.payload
  if (event.event_type === 'user.message') return <article className="coordinator-event user"><div className="coordinator-event-label">用户消息 · #{event.sequence}</div><MarkdownContent content={stringValue(payload.message) ?? ''} /></article>
  if (event.event_type === 'activation.started') return <article className="coordinator-event system"><div className="coordinator-event-label"><LoaderCircle size={13} className="spin" /> Coordinator 激活开始</div><small>activation {stringValue(payload.activation_id)}</small></article>
  if (event.event_type === 'activation.completed') return <article className="coordinator-event coordinator"><div className="coordinator-event-label"><CircleCheck size={13} /> Coordinator 激活完成</div><p>{stringValue(payload.message) ?? '本轮没有附加消息。'}</p><small>{stringValue(payload.outcome)} · {String(payload.step_count ?? '—')} steps</small></article>
  if (event.event_type === 'coordinator.decision') { const decision = asRecord(payload.decision); return <article className="coordinator-event decision"><div className="coordinator-event-label"><BrainCircuit size={13} />决策 · {stringValue(decision?.kind) ?? 'unknown'}</div><p>{stringValue(decision?.rationale) ?? '已产生结构化调度决策。'}</p><small>target {stringValue(decision?.target_node_id) ?? '—'}</small></article> }
  if (event.event_type === 'delegation.event') { const source = asRecord(payload.source); return <article className="coordinator-event delegation"><div className="coordinator-event-label"><GitBranch size={13} />委派状态更新 · {stringValue(payload.delegation_id)}</div><p>{stringValue(source?.status) ?? stringValue(source?.kind) ?? '事件已同步'}</p><small>节点 {stringValue(payload.node_id) ?? '—'} · V3 sequence {String(source?.sequence ?? '—')}</small></article> }
  if (event.event_type === 'approval.resolved') return <article className="coordinator-event approval"><div className="coordinator-event-label"><ShieldCheck size={13} />审批状态已更新</div><p>保护操作的审批结果已写入 Coordinator 会话。</p></article>
  return <article className="coordinator-event system"><div className="coordinator-event-label"><CircleAlert size={13} />{event.event_type} · #{event.sequence}</div><pre>{JSON.stringify(payload, null, 2)}</pre></article>
}

function CoordinatorPlan({ session, onApprovalResolved }: { session: CoordinatorSessionDomain; onApprovalResolved: () => void }) {
  const plan = session.plan
  const approvals = session.autonomy.approvals.filter((approval) => approval.status === 'pending')
  return <div className="coordinator-plan-content">{plan === null ? <CoordinatorEmpty icon={<GitBranch />} title="尚未形成计划" description="发送目标后，Coordinator 会先理解目标，再决定是否创建计划和派遣节点。" /> : <><div className="coordinator-plan-summary"><CoordinatorStatus status={plan.status} /><span>{plan.nodes.length} 个节点</span><span>plan {plan.plan_id}</span></div><div className="coordinator-node-list">{plan.nodes.map((node) => <CoordinatorNode node={node} key={node.node_id} />)}</div></>}{approvals.length > 0 && <div className="coordinator-approvals"><div className="coordinator-subtitle"><ShieldCheck size={14} />待处理审批</div>{approvals.map((approval) => <ApprovalCard approval={approval} key={String(approval.approval_id)} session={session} onResolved={onApprovalResolved} />)}</div>}<details className="coordinator-debug"><summary>会话元数据</summary><dl><div><dt>session</dt><dd>{session.session_id}</dd></div><div><dt>cognitive</dt><dd>{session.cognitive_session_id}</dd></div><div><dt>revision</dt><dd>{session.revision}</dd></div></dl></details></div>
}

function CoordinatorNode({ node }: { node: CoordinatorPlanNode }) {
  return <article className="coordinator-node"><div className="coordinator-node-head"><CoordinatorStatus status={node.status} /><strong>{node.intent.objective}</strong></div><div className="coordinator-node-meta"><span>{node.node_id} · attempt {node.attempt}</span>{node.selection && <span>{node.selection.provider_id} / {node.selection.model_id}</span>}</div>{node.execution && <code>delegation {node.execution.delegation_id}</code>}{node.intent.acceptance_criteria.length > 0 && <ul>{node.intent.acceptance_criteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul>}</article>
}

function ApprovalCard({ approval, session, onResolved }: { approval: Record<string, unknown>; session: CoordinatorSessionDomain; onResolved: () => void }) {
  const [pending, setPending] = useState(false)
  const resolve = async (approved: boolean) => { setPending(true); try { await api.resolveCoordinatorApproval(session.session_id, String(approval.approval_id), { approved, actor_id: 'local-user', reason: approved ? '页面批准' : '页面拒绝', expected_session_revision: session.revision }); onResolved() } finally { setPending(false) } }
  return <div className="coordinator-approval-card"><strong>{String(approval.reason ?? '需要人工审批')}</strong><small>{String(approval.action_key ?? approval.approval_id)}</small><div><button className="secondary-button" onClick={() => void resolve(false)} disabled={pending}>拒绝</button><button className="primary-button" onClick={() => void resolve(true)} disabled={pending}>批准</button></div></div>
}

function CoordinatorComposer({ onClose, onCreated }: { onClose: () => void; onCreated: (sessionId: string) => void }) {
  const [sessionId, setSessionId] = useState('coordinator-' + Date.now().toString(36))
  const [cwd, setCwd] = useState('')
  const [prompt, setPrompt] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string>()
  const submit = async (event: FormEvent) => { event.preventDefault(); if (!sessionId.trim() || !cwd.trim() || !prompt.trim() || pending) return; setPending(true); try { await api.createCoordinatorSession({ session_id: sessionId.trim(), cwd: cwd.trim(), prompt: prompt.trim() }); onCreated(sessionId.trim()) } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) } finally { setPending(false) } }
  return <div className="modal-backdrop"><form className="modal coordinator-composer-modal" onSubmit={submit}><div className="modal-header"><div><span className="eyebrow">NEW COORDINATOR SESSION</span><h2>创建持续会话</h2></div><button type="button" className="icon-button" onClick={onClose}><X size={16} /></button></div><label>会话 ID<input value={sessionId} onChange={(event) => setSessionId(event.target.value)} /></label><label>工作目录<input value={cwd} onChange={(event) => setCwd(event.target.value)} placeholder="例如 D:\\dev\\AITools\\multi-agent-v3" /></label><label>目标<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={5} placeholder="描述需要持续推进的复杂目标…" /></label>{error && <div className="error-banner">创建失败：{error}</div>}<div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button type="submit" className="primary-button" disabled={pending || !cwd.trim() || !prompt.trim()}>{pending ? <LoaderCircle size={15} className="spin" /> : <Plus size={15} />}创建并激活</button></div></form></div>
}

function CoordinatorEmpty({ icon, title, description }: { icon: ReactNode; title: string; description?: string }) {
  return <div className="coordinator-empty"><div>{icon}</div><strong>{title}</strong>{description && <p>{description}</p>}</div>
}

function CoordinatorStatus({ status }: { status: string }) {
  return <span className={'coordinator-status ' + status}><span />{statusLabels[status] ?? status}</span>
}

function connectionLabel(connection: ConnectionState): string {
  if (connection === 'connected') return '实时连接'
  if (connection === 'connecting') return '正在连接'
  if (connection === 'reconnecting') return '等待重连'
  return '连接已结束'
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
