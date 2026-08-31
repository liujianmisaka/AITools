import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  Archive,
  BrainCircuit,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Clock3,
  GitBranch,
  LoaderCircle,
  MessageSquareText,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldCheck,
  Square,
  X,
} from 'lucide-react'
import { api, coordinatorStreamUrl } from './api'
import { shouldDisplayCoordinatorEvent } from './coordinator-event-projection'
import { MarkdownContent } from './MarkdownContent'
import './coordinator-event.css'
import type {
  CoordinatorEvent,
  CoordinatorNodeSnapshot,
  CoordinatorPlanNode,
  CoordinatorSession,
  CoordinatorSessionDomain,
  CoordinatorSessionSummary,
  Delegation,
} from './types'

const statusLabels: Record<string, string> = {
  active: '执行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  draft: '计划草稿',
  ready: '待派遣',
  review_required: '待验收',
  running: '执行中',
  waiting: '等待事件',
  reviewing: '待验收',
  reconciliation_required: '待对账',
  accepted: '已验收',
  awaiting_event: '等待事件',
  delegated: '已派遣',
}

type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'ended'
type SessionView = 'active' | 'archived'

const COORDINATOR_ID = 'multi-agent-coordinator'

function canArchiveSession(session: CoordinatorSessionSummary): boolean {
  return session.archived || session.archivable
}

function archiveActionTitle(session: CoordinatorSessionSummary): string {
  if (session.archived) return '恢复会话'
  if (session.archivable) return '归档会话'
  if (session.archive_blocker === 'pending_event_activation') {
    return 'Coordinator 正在处理待触发事件，完成后才能归档'
  }
  if (session.archive_blocker === 'active_work') {
    return '会话仍有活动工作，结束或取消后才能归档'
  }
  return '当前状态不允许归档'
}

function isSessionReadOnly(session: CoordinatorSessionDomain): boolean {
  return session.archived_at !== null || session.goal?.status !== 'active'
}

function readCoordinatorRoute(): { coordinatorId: string | null; sessionId: string | null } {
  const match = window.location.hash.match(/^#\/coordinators\/([^/]+)(?:\/sessions\/(.+))?$/)
  if (match === null) return { coordinatorId: null, sessionId: null }
  try {
    return {
      coordinatorId: decodeURIComponent(match[1]),
      sessionId: match[2] ? decodeURIComponent(match[2]) : null,
    }
  } catch {
    return { coordinatorId: null, sessionId: null }
  }
}

function coordinatorRoute(sessionId: string | null): string {
  const base = '#/coordinators/' + encodeURIComponent(COORDINATOR_ID)
  return sessionId === null ? base : base + '/sessions/' + encodeURIComponent(sessionId)
}

function writeCoordinatorRoute(sessionId: string | null, replace: boolean): void {
  const next = coordinatorRoute(sessionId)
  if (window.location.hash === next.slice(1)) return
  window.history[replace ? 'replaceState' : 'pushState']({}, '', next)
}

export function CoordinatorPage({
  onOpenDelegation,
}: {
  onOpenDelegation: (delegationId: string) => void
}) {
  const [activeSessions, setActiveSessions] = useState<CoordinatorSessionSummary[]>([])
  const [archivedSessions, setArchivedSessions] = useState<CoordinatorSessionSummary[]>([])
  const [sessionView, setSessionView] = useState<SessionView>('active')
  const [selectedId, setSelectedId] = useState<string | null>(() => readCoordinatorRoute().sessionId)
  const [coordinatorExpanded, setCoordinatorExpanded] = useState(true)
  const [record, setRecord] = useState<CoordinatorSession | null>(null)
  const [events, setEvents] = useState<CoordinatorEvent[]>([])
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [error, setError] = useState<string>()
  const [loading, setLoading] = useState(true)
  const [sessionsLoadFailed, setSessionsLoadFailed] = useState(false)
  const [composerOpen, setComposerOpen] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)
  const [sessionActionId, setSessionActionId] = useState<string>()
  const [delegationSnapshots, setDelegationSnapshots] = useState<Record<string, Delegation>>({})
  const sessions = sessionView === 'active' ? activeSessions : archivedSessions

  const refreshSessions = useCallback(async () => {
    setLoading(true)
    try {
      const [nextActive, nextArchived] = await Promise.all([
        api.coordinatorSessions(false),
        api.coordinatorSessions(true),
      ])
      setActiveSessions(nextActive)
      setArchivedSessions(nextArchived)
      setSessionsLoadFailed(false)
      setError(undefined)
    } catch (reason) {
      setSessionsLoadFailed(true)
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshSessions()
  }, [refreshSessions, refreshToken])

  useEffect(() => {
    const syncRoute = () => {
      const route = readCoordinatorRoute()
      if (route.coordinatorId === null || route.coordinatorId === COORDINATOR_ID) {
        setSelectedId(route.sessionId)
      }
    }
    window.addEventListener('hashchange', syncRoute)
    window.addEventListener('popstate', syncRoute)
    return () => {
      window.removeEventListener('hashchange', syncRoute)
      window.removeEventListener('popstate', syncRoute)
    }
  }, [])

  useEffect(() => {
    if (loading || sessionsLoadFailed) return
    if (selectedId !== null && sessions.some((session) => session.session_id === selectedId)) return
    if (selectedId !== null) {
      const targetView = activeSessions.some((session) => session.session_id === selectedId)
        ? 'active'
        : archivedSessions.some((session) => session.session_id === selectedId)
          ? 'archived'
          : null
      if (targetView !== null && targetView !== sessionView) {
        setSessionView(targetView)
        return
      }
    }
    const fallbackId = sessions[0]?.session_id ?? null
    setSelectedId(fallbackId)
    writeCoordinatorRoute(fallbackId, true)
  }, [activeSessions, archivedSessions, loading, selectedId, sessionView, sessions, sessionsLoadFailed])

  const selectSession = useCallback((sessionId: string | null, replace = false) => {
    setSelectedId(sessionId)
    writeCoordinatorRoute(sessionId, replace)
  }, [])

  const selectSessionView = useCallback((nextView: SessionView) => {
    const nextSessions = nextView === 'active' ? activeSessions : archivedSessions
    setSessionView(nextView)
    selectSession(nextSessions[0]?.session_id ?? null, true)
  }, [activeSessions, archivedSessions, selectSession])

  const changeSessionArchive = useCallback(async (sessionId: string, archive: boolean) => {
    if (sessionActionId !== undefined) return
    setSessionActionId(sessionId)
    try {
      if (archive) await api.archiveCoordinatorSession(sessionId)
      else await api.unarchiveCoordinatorSession(sessionId)
      if (!archive) {
        setSessionView('active')
        selectSession(sessionId, true)
      } else if (selectedId === sessionId) {
        setRecord(null)
        setEvents([])
        setDelegationSnapshots({})
        selectSession(null, true)
      }
      await refreshSessions()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSessionActionId(undefined)
    }
  }, [refreshSessions, selectSession, selectedId, sessionActionId])

  useEffect(() => {
    if (selectedId === null) {
      setRecord(null)
      setEvents([])
      setDelegationSnapshots({})
      setConnection('ended')
      return
    }
    const activeSessionId = selectedId
    setRecord(null)
    setEvents([])
    setDelegationSnapshots({})
    setConnection('connecting')
    let disposed = false
    let source: EventSource | null = null
    let reconnectTimer: number | undefined
    let lastSequence = 0
    let currentEvents: CoordinatorEvent[] = []
    let connectionState: ConnectionState = 'connecting'
    let refreshPromise: Promise<boolean> | null = null

    const updateConnection = (next: ConnectionState) => {
      connectionState = next
      if (!disposed) setConnection(next)
    }

    const mergeEvents = (incoming: CoordinatorEvent[]): boolean => {
      for (const event of incoming) {
        lastSequence = Math.max(lastSequence, event.sequence)
      }
      const visibleIncoming = incoming.filter(shouldDisplayCoordinatorEvent)
      if (visibleIncoming.length === 0) return false
      const bySequence = new Map<number, CoordinatorEvent>()
      for (const event of [...currentEvents, ...visibleIncoming]) {
        bySequence.set(event.sequence, event)
      }
      currentEvents = [...bySequence.values()].sort((left, right) => left.sequence - right.sequence).slice(-800)
      if (!disposed) setEvents(currentEvents)
      return true
    }

    const refreshRecord = (includeEvents: boolean): Promise<boolean> => {
      if (refreshPromise !== null) return refreshPromise
      const run = async (): Promise<boolean> => {
        try {
          const [nextRecord, nextEvents] = await Promise.all([
            api.coordinatorSession(activeSessionId),
            includeEvents
              ? api.coordinatorEvents(activeSessionId, lastSequence + 1)
              : Promise.resolve([]),
          ])
          if (disposed) return false
          setRecord(nextRecord)
          if (nextEvents.length > 0) mergeEvents(nextEvents)
          setError(undefined)
          return true
        } catch (reason) {
          if (!disposed) {
            setError(reason instanceof Error ? reason.message : String(reason))
          }
          return false
        }
      }
      const nextPromise = run()
      refreshPromise = nextPromise
      void nextPromise.then(
        () => {
          if (refreshPromise === nextPromise) refreshPromise = null
        },
        () => {
          if (refreshPromise === nextPromise) refreshPromise = null
        },
      )
      return nextPromise
    }

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== undefined) return
      updateConnection('reconnecting')
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined
        openStream(lastSequence + 1)
      }, 3_000)
    }

    const openStream = (nextSequence: number) => {
      if (disposed) return
      source?.close()
      updateConnection('connecting')
      const nextSource = new EventSource(coordinatorStreamUrl(activeSessionId, nextSequence))
      source = nextSource
      nextSource.onopen = () => {
        if (!disposed && source === nextSource) {
          updateConnection('connected')
          setError(undefined)
        }
      }
      nextSource.onerror = () => {
        if (!disposed && source === nextSource) {
          nextSource.close()
          source = null
          scheduleReconnect()
        }
      }
      nextSource.addEventListener('coordinator.session.event', (event) => {
        if (disposed || source !== nextSource) return
        try {
          const next = JSON.parse((event as MessageEvent<string>).data) as CoordinatorEvent
          if (mergeEvents([next])) void refreshRecord(false)
        } catch (reason) {
          if (!disposed) setError(reason instanceof Error ? reason.message : 'Coordinator 事件格式无效')
        }
      })
      nextSource.addEventListener('coordinator.session.end', () => {
        if (!disposed && source === nextSource) {
          nextSource.close()
          source = null
          void refreshRecord(true)
          scheduleReconnect()
        }
      })
    }

    const initialize = async () => {
      const loaded = await refreshRecord(true)
      if (disposed) return
      if (loaded) openStream(lastSequence + 1)
      else scheduleReconnect()
    }
    void initialize()
    const fallbackTimer = window.setInterval(() => {
      if (!disposed) {
        void refreshRecord(connectionState !== 'connected').then((loaded) => {
          if (!loaded) scheduleReconnect()
        })
      }
    }, 5_000)
    return () => {
      disposed = true
      source?.close()
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer)
      window.clearInterval(fallbackTimer)
    }
  }, [selectedId])

  useEffect(() => {
    if (selectedId === null || refreshToken === 0) return
    let disposed = false
    void api.coordinatorSession(selectedId).then((nextRecord) => {
      if (!disposed) setRecord(nextRecord)
    }).catch((reason) => {
      if (!disposed) setError(reason instanceof Error ? reason.message : String(reason))
    })
    return () => {
      disposed = true
    }
  }, [selectedId, refreshToken])

  useEffect(() => {
    let disposed = false
    const sessionId = record?.session.session_id ?? null
    const hasPlan = record?.session.plan != null
    if (sessionId === null || !hasPlan) {
      setDelegationSnapshots({})
      return
    }
    let requestInFlight = false
    const refreshSnapshots = async () => {
      if (disposed || requestInFlight) return
      requestInFlight = true
      try {
        const entries = await api.coordinatorNodeSnapshots(sessionId)
        if (disposed) return
        setDelegationSnapshots(
          Object.fromEntries(
            entries.map((entry: CoordinatorNodeSnapshot) => [
              entry.snapshot.delegation_id,
              entry.snapshot,
            ]),
          ),
        )
      } catch (reason) {
        if (!disposed) setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        requestInFlight = false
      }
    }
    void refreshSnapshots()
    const snapshotTimer = window.setInterval(() => {
      void refreshSnapshots()
    }, 5_000)
    return () => {
      disposed = true
      window.clearInterval(snapshotTimer)
    }
  }, [record?.session.session_id, record?.session.plan != null])

  const selectedSummary = useMemo(() => sessions.find((session) => session.session_id === selectedId) ?? null, [selectedId, sessions])
  const activeSessionCount = useMemo(() => activeSessions.filter((session) => ['active', 'running', 'waiting', 'reviewing'].includes(session.plan_status ?? session.goal?.status ?? '')).length, [activeSessions])

  return (
    <div className="coordinator-page">
      <section className="panel coordinator-pane coordinator-sessions-pane">
        <div className="panel-header coordinator-pane-header">
          <div><span className="eyebrow">COORDINATOR</span><h2>Coordinator</h2><p>单一主控 · {activeSessions.length} 个会话 · {archivedSessions.length} 已归档</p></div>
          <div className="panel-tools"><button className="icon-button" onClick={() => setRefreshToken((value) => value + 1)} title="刷新"><RefreshCw size={16} /></button><button className="icon-button" onClick={() => setComposerOpen(true)} title="新建会话"><Plus size={16} /></button></div>
        </div>
        {error && <div className="error-banner coordinator-error">读取 Coordinator 失败：{error}</div>}
        <div className="coordinator-tree">
          <button className={'coordinator-root-row ' + (coordinatorExpanded ? 'expanded' : '')} type="button" onClick={() => setCoordinatorExpanded((value) => !value)} aria-expanded={coordinatorExpanded}>
            <span className="coordinator-root-leading"><span className="coordinator-root-icon"><BrainCircuit size={16} /></span><span><strong>Local Coordinator</strong><small>{COORDINATOR_ID}</small></span></span>
            <span className="coordinator-root-meta"><span className="coordinator-online"><span />在线</span><ChevronRight size={14} /></span>
          </button>
          {coordinatorExpanded && <div className="coordinator-session-group">
            <div className="coordinator-session-tabs" role="tablist" aria-label="Coordinator 会话筛选">
              <button type="button" role="tab" aria-selected={sessionView === 'active'} className={sessionView === 'active' ? 'active' : ''} onClick={() => selectSessionView('active')}><MessageSquareText size={12} />会话 <span>{activeSessions.length}</span></button>
              <button type="button" role="tab" aria-selected={sessionView === 'archived'} className={sessionView === 'archived' ? 'active' : ''} onClick={() => selectSessionView('archived')}><Archive size={12} />已归档 <span>{archivedSessions.length}</span></button>
            </div>
            <div className="coordinator-session-group-header"><span>{sessionView === 'active' ? '会话' : '已归档'}</span><span>{sessionView === 'active' ? `${activeSessionCount} 执行中 · ${sessions.length} 总计` : `${sessions.length} 个归档会话`}</span></div>
            {loading ? <CoordinatorEmpty icon={<LoaderCircle className="spin" />} title="正在加载会话" /> : sessions.length === 0 ? <CoordinatorEmpty icon={sessionView === 'active' ? <MessageSquareText /> : <Archive />} title={sessionView === 'active' ? '还没有会话' : '还没有归档会话'} description={sessionView === 'active' ? '创建会话后，它会出现在当前 Coordinator 下。' : '已结束的会话可以从会话列表归档到这里。'} /> : <div className="coordinator-session-list">{sessions.map((session) => { const archivable = canArchiveSession(session); return <div className="coordinator-session-row-shell" key={session.session_id}><button type="button" className={'coordinator-session-row ' + (session.session_id === selectedId ? 'selected' : '')} onClick={() => selectSession(session.session_id)}><div className="coordinator-session-row-head"><CoordinatorStatus status={session.plan_status ?? session.goal?.status ?? 'active'} /><span>rev {session.revision}</span></div><strong>{session.goal?.objective ?? '未设置目标'}</strong><small>{session.session_id}</small></button><button type="button" className="coordinator-session-archive-action" onClick={() => void changeSessionArchive(session.session_id, !session.archived)} disabled={sessionActionId !== undefined || !archivable} title={archiveActionTitle(session)} aria-label={session.archived ? `恢复 ${session.goal?.objective ?? session.session_id}` : `归档 ${session.goal?.objective ?? session.session_id}`}>{sessionActionId === session.session_id ? <LoaderCircle size={13} className="spin" /> : session.archived ? <RotateCcw size={13} /> : <Archive size={13} />}</button></div> })}</div>}
          </div>}
        </div>
      </section>

      <section className="panel coordinator-pane coordinator-conversation-pane">
        <div className="panel-header coordinator-pane-header coordinator-conversation-header"><div><span className="eyebrow">PRIMARY COORDINATOR</span><h2>{selectedSummary?.goal?.objective ?? selectedId ?? '选择一个会话'}</h2><p>{selectedId ?? '选择左侧会话开始协作'}</p></div><div className="coordinator-conversation-header-meta">{selectedSummary && <button type="button" className="secondary-button coordinator-session-header-action" onClick={() => void changeSessionArchive(selectedSummary.session_id, !selectedSummary.archived)} disabled={sessionActionId !== undefined || !canArchiveSession(selectedSummary)} title={archiveActionTitle(selectedSummary)}>{selectedSummary.archived ? <RotateCcw size={12} /> : <Archive size={12} />}{selectedSummary.archived ? '恢复' : '归档'}</button>}<span className="coordinator-mode-chip"><BrainCircuit size={12} />单一 Coordinator 主控</span><div className={'coordinator-connection ' + connection}><span />{connectionLabel(connection)}</div></div></div>
        {record === null ? <CoordinatorEmpty icon={<MessageSquareText />} title="选择一个会话查看对话" /> : <CoordinatorConversation events={events} session={record.session} onMessage={async (message) => { await api.sendCoordinatorMessage(record.session.session_id, message); setRefreshToken((value) => value + 1) }} onCancel={async () => { await api.cancelCoordinatorSession(record.session.session_id, '用户从 Coordinator 页面取消目标'); setRefreshToken((value) => value + 1) }} />}
      </section>

      <section className="panel coordinator-pane coordinator-plan-pane">
        <div className="panel-header coordinator-pane-header"><div><span className="eyebrow">CURRENT SESSION TASKS</span><h2>当前会话任务</h2><p>{record?.session.plan ? '当前 Coordinator 正在拆解并推进任务' : '等待 Coordinator 形成任务计划'}</p></div><GitBranch size={18} className="coordinator-panel-icon" /></div>
        {record === null ? <CoordinatorEmpty icon={<GitBranch />} title="暂无计划" /> : <CoordinatorPlan session={record.session} delegationSnapshots={delegationSnapshots} onOpenDelegation={onOpenDelegation} onChanged={() => setRefreshToken((value) => value + 1)} onApprovalResolved={() => setRefreshToken((value) => value + 1)} />}
      </section>
      {composerOpen && <CoordinatorComposer onClose={() => setComposerOpen(false)} onCreated={(sessionId) => { setComposerOpen(false); setSessionView('active'); selectSession(sessionId); setRefreshToken((value) => value + 1) }} />}
    </div>
  )
}

function CoordinatorConversation({ events, session, onMessage, onCancel }: { events: CoordinatorEvent[]; session: CoordinatorSessionDomain; onMessage: (message: string) => Promise<void>; onCancel: () => Promise<void> }) {
  const [message, setMessage] = useState('')
  const [pending, setPending] = useState(false)
  const transcriptRef = useRef<HTMLDivElement>(null)
  useEffect(() => { const element = transcriptRef.current; if (element !== null) element.scrollTop = element.scrollHeight }, [events.length])
  const archived = session.archived_at !== null
  const readOnly = isSessionReadOnly(session)
  const submit = async (event: FormEvent) => { event.preventDefault(); const next = message.trim(); if (!next || pending || readOnly) return; setPending(true); try { await onMessage(next); setMessage('') } finally { setPending(false) } }
  const canCancel = !archived && session.goal?.status === 'active'
  const placeholder = archived
    ? '该会话已归档，当前为只读状态。'
    : readOnly
      ? '当前目标已结束，历史会话保持只读。'
      : '继续补充目标、回答 Coordinator 的问题，或要求重新规划…'
  const stateLabel = archived
    ? '已归档 · 当前为只读状态'
    : readOnly
      ? `目标状态：${statusLabels[session.goal?.status ?? ''] ?? '已结束'} · 当前为只读状态`
      : `当前目标：${session.goal?.status ?? '未开始'} · revision ${session.revision}`
  return <div className="coordinator-conversation-content"><div className="coordinator-transcript" ref={transcriptRef}>{events.length === 0 ? <CoordinatorEmpty icon={<Clock3 />} title="等待事件" description="Coordinator 激活后，用户消息、计划决策和委派状态会显示在这里。" /> : events.map((event) => <CoordinatorEventCard event={event} key={event.event_id} />)}</div><form className={'coordinator-composer ' + (readOnly ? 'archived' : '')} onSubmit={submit}><textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder={placeholder} disabled={pending || readOnly} rows={3} /><div className="coordinator-composer-actions"><span>{stateLabel}</span><div>{canCancel && <button type="button" className="danger-button" onClick={() => { if (!pending) { setPending(true); void onCancel().finally(() => setPending(false)) } }} disabled={pending}><Square size={14} />取消目标</button>}<button type="submit" className="primary-button" disabled={readOnly || pending || message.trim().length === 0}><Send size={14} />发送</button></div></div></form></div>
}

function CoordinatorEventCard({ event }: { event: CoordinatorEvent }) {
  const payload = event.payload
  if (event.event_type === 'user.message') return <article className="coordinator-event user"><div className="coordinator-event-label">用户消息 · #{event.sequence}</div><MarkdownContent content={stringValue(payload.message) ?? ''} /></article>
  if (event.event_type === 'activation.started') return <article className="coordinator-event system"><div className="coordinator-event-label"><LoaderCircle size={13} className="spin" /> Coordinator 激活开始</div><small>activation {stringValue(payload.activation_id)}</small></article>
  if (event.event_type === 'activation.completed') return <article className="coordinator-event coordinator"><div className="coordinator-event-label"><CircleCheck size={13} /> Coordinator 激活完成</div><p>{activationCompletionMessage(payload)}</p><small>{stringValue(payload.outcome)} · {String(payload.step_count ?? '—')} steps</small></article>
  if (event.event_type === 'activation.failed') return <article className="coordinator-event error"><div className="coordinator-event-label"><CircleAlert size={13} /> Coordinator 激活失败</div><p>{stringValue(payload.error) ?? '模型决策或编排执行失败。'}</p><small>activation {stringValue(payload.activation_id) ?? '—'}</small></article>
  if (event.event_type === 'coordinator.decision') { const decision = asRecord(payload.decision); return <article className="coordinator-event decision"><div className="coordinator-event-label"><BrainCircuit size={13} />决策 · {stringValue(decision?.kind) ?? 'unknown'}</div><p>{stringValue(decision?.rationale) ?? '已产生结构化调度决策。'}</p><small>target {stringValue(decision?.target_node_id) ?? '—'}</small></article> }
  if (event.event_type === 'delegation.event') return <CoordinatorDelegationEvent event={event} />
  if (event.event_type === 'approval.resolved') return <article className="coordinator-event approval"><div className="coordinator-event-label"><ShieldCheck size={13} />审批状态已更新</div><p>保护操作的审批结果已写入 Coordinator 会话。</p></article>
  if (event.event_type === 'session.cancelled') return <article className="coordinator-event error"><div className="coordinator-event-label"><CircleAlert size={13} />目标已取消</div><p>{stringValue(payload.reason) ?? 'Coordinator 目标已取消。'}</p></article>
  if (event.event_type === 'session.archived') return <article className="coordinator-event system"><div className="coordinator-event-label"><Archive size={13} />会话已归档</div><p>历史对话和任务记录保持可读。</p></article>
  if (event.event_type === 'session.unarchived') return <article className="coordinator-event system"><div className="coordinator-event-label"><RotateCcw size={13} />会话已恢复</div><p>该 Coordinator 会话已恢复到会话列表；目标终态仍保持只读。</p></article>
  return <article className="coordinator-event system"><div className="coordinator-event-label"><CircleAlert size={13} />系统事件 · {event.event_type}</div><details><summary>查看原始事件</summary><pre>{JSON.stringify(payload, null, 2)}</pre></details></article>
}

function CoordinatorDelegationEvent({ event }: { event: CoordinatorEvent }) {
  const source = asRecord(event.payload.source)
  const providerPayload = asRecord(source?.payload)
  const kind = stringValue(source?.kind)
  const status = stringValue(source?.status) ?? 'unknown'
  const nodeId = stringValue(event.payload.node_id) ?? '未知任务'
  const delegationId = stringValue(event.payload.delegation_id) ?? '未知委派'

  if (kind === 'output_completed') {
    return <article className="coordinator-event delegation"><div className="coordinator-event-label"><MessageSquareText size={13} /> Agent 进展 · {nodeId}</div><MarkdownContent content={stringValue(providerPayload?.text) ?? ''} /><small>{delegationId}</small></article>
  }
  if (status === 'active') {
    const provider = stringValue(providerPayload?.provider_id) ?? 'Agent'
    const model = stringValue(providerPayload?.model)
    const effort = stringValue(providerPayload?.effort)
    return <article className="coordinator-event delegation"><div className="coordinator-event-label"><GitBranch size={13} />委派已启动 · {nodeId}</div><p>{provider}{model ? ` / ${model}` : ''}{effort ? ` · ${effort}` : ''}</p><small>{delegationId}</small></article>
  }
  const detail = stringValue(providerPayload?.error_message) ?? stringValue(providerPayload?.reason) ?? delegationStatusMessage(status)
  return <article className={'coordinator-event delegation ' + status}><div className="coordinator-event-label"><CircleAlert size={13} />{delegationStatusTitle(status)} · {nodeId}</div><p>{detail}</p><small>{delegationId}</small></article>
}

function activationCompletionMessage(payload: Record<string, unknown>): string {
  const message = stringValue(payload.message)
  if (message) return message
  const outcome = stringValue(payload.outcome)
  if (outcome === 'waiting') return '本轮决策已完成，正在等待委派事件或用户输入。'
  if (outcome === 'input_required') return 'Coordinator 需要用户输入后才能继续。'
  if (outcome === 'completed') return 'Coordinator 已完成当前目标。'
  return '本轮 Coordinator 决策已完成。'
}

function delegationStatusTitle(status: string): string {
  if (status === 'completed') return '委派已完成'
  if (status === 'failed') return '委派执行失败'
  if (status === 'cancelled') return '委派已取消'
  if (status === 'reconciliation_required') return '委派需要人工对账'
  if (status === 'waiting_input') return '委派等待输入'
  if (status === 'paused') return '委派已暂停'
  return `委派状态：${status}`
}

function delegationStatusMessage(status: string): string {
  if (status === 'completed') return 'Agent 已返回结果，等待 Coordinator 审查或验收。'
  if (status === 'failed') return 'Agent 执行失败，请打开任务详情查看错误与重试入口。'
  if (status === 'cancelled') return '该委派已经停止执行。'
  if (status === 'reconciliation_required') return '缺少可信终态，需要人工确认实际执行结果。'
  if (status === 'waiting_input') return 'Agent 正在等待补充信息或人工操作。'
  if (status === 'paused') return 'Agent 当前暂停，等待后续恢复。'
  return status
}

function CoordinatorPlan({
  session,
  delegationSnapshots,
  onOpenDelegation,
  onChanged,
  onApprovalResolved,
}: {
  session: CoordinatorSessionDomain
  delegationSnapshots: Record<string, Delegation>
  onOpenDelegation: (delegationId: string) => void
  onChanged: () => void
  onApprovalResolved: () => void
}) {
  const plan = session.plan
  const archived = session.archived_at !== null
  const readOnly = isSessionReadOnly(session)
  const approvals = session.autonomy.approvals.filter((approval) => approval.status === 'pending')
  const displayPlanStatus = plan === null ? null : coordinatorPlanDisplayStatus(plan.status, plan.nodes)
  return <div className="coordinator-plan-content">{plan === null ? <CoordinatorEmpty icon={<GitBranch />} title="尚未形成任务" description="发送目标后，Coordinator 会在当前会话中拆解、派遣并跟踪任务。" /> : <><div className="coordinator-plan-summary"><CoordinatorStatus status={displayPlanStatus ?? plan.status} /><span>{plan.nodes.length} 个任务</span><span>plan {plan.plan_id}</span>{readOnly && <span>{archived ? '只读归档' : '目标已结束'}</span>}</div><CoordinatorTaskSummary nodes={plan.nodes} /><div className="coordinator-node-list">{plan.nodes.map((node) => <CoordinatorNode node={node} session={session} delegation={node.execution ? delegationSnapshots[node.execution.delegation_id] : undefined} readOnly={readOnly} onOpenDelegation={onOpenDelegation} onChanged={onChanged} key={node.node_id} />)}</div></>}{!readOnly && approvals.length > 0 && <div className="coordinator-approvals"><div className="coordinator-subtitle"><ShieldCheck size={14} />待处理审批</div>{approvals.map((approval) => <ApprovalCard approval={approval} key={String(approval.approval_id)} session={session} onResolved={onApprovalResolved} />)}</div>}<details className="coordinator-debug"><summary>会话元数据</summary><dl><div><dt>session</dt><dd>{session.session_id}</dd></div><div><dt>cognitive</dt><dd>{session.cognitive_session_id}</dd></div><div><dt>revision</dt><dd>{session.revision}</dd></div><div><dt>archived</dt><dd>{session.archived_at ?? '否'}</dd></div></dl></details></div>
}

function CoordinatorTaskSummary({ nodes }: { nodes: CoordinatorPlanNode[] }) {
  const completed = nodes.filter((node) => ['accepted', 'completed'].includes(node.status)).length
  const active = nodes.filter((node) => ['delegated', 'awaiting_event', 'active'].includes(node.status)).length
  const attention = nodes.filter((node) => ['reconciliation_required', 'review_required', 'failed'].includes(node.status)).length
  return <div className="coordinator-task-summary"><span><strong>{nodes.length}</strong>全部任务</span><span><strong>{active}</strong>执行中</span><span><strong>{completed}</strong>已完成</span>{attention > 0 && <span className="attention"><strong>{attention}</strong>需处理</span>}</div>
}

function coordinatorPlanDisplayStatus(status: string, nodes: CoordinatorPlanNode[]): string {
  if (['completed', 'failed', 'cancelled'].includes(status)) return status
  const nodeStatuses = new Set(nodes.map((node) => node.status))
  if (nodeStatuses.has('reconciliation_required')) return 'reconciliation_required'
  if (nodeStatuses.has('review_required')) return 'review_required'
  if (status === 'reviewing' && !nodeStatuses.has('review_required')) {
    if (nodeStatuses.has('failed')) return 'failed'
    if (nodeStatuses.has('cancelled')) return 'cancelled'
  }
  return status
}

function CoordinatorNode({
  node,
  session,
  delegation,
  readOnly,
  onOpenDelegation,
  onChanged,
}: {
  node: CoordinatorPlanNode
  session: CoordinatorSessionDomain
  delegation?: Delegation
  readOnly: boolean
  onOpenDelegation: (delegationId: string) => void
  onChanged: () => void
}) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string>()
  const [supplement, setSupplement] = useState('')
  const [reconcileOpen, setReconcileOpen] = useState(false)
  const [reconcileStatus, setReconcileStatus] = useState<'completed' | 'failed' | 'cancelled'>('failed')
  const [reconcileReason, setReconcileReason] = useState('')
  const displayStatus = delegation?.status ?? node.status
  const canAccept = !readOnly && displayStatus === 'completed'
  const canReconcile = !readOnly && (displayStatus === 'reconciliation_required' || node.status === 'reconciliation_required')
  const canRetry = !readOnly && (['failed', 'review_required'].includes(displayStatus) || ['failed', 'review_required'].includes(node.status))
  const canSupplement = !readOnly && ['active', 'paused', 'waiting_input', 'completed'].includes(displayStatus)

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

  return <article className="coordinator-node"><div className="coordinator-node-head"><CoordinatorStatus status={displayStatus} /><strong>{node.intent.objective}</strong></div><div className="coordinator-node-meta"><span>{node.node_id} · attempt {node.attempt}</span>{node.selection && <span>{node.selection.provider_id} / {node.selection.model_id}</span>}{delegation?.report?.error_code && <span className="coordinator-node-error-code">{delegation.report.error_code}</span>}</div>{node.execution && <button type="button" className="coordinator-delegation-link" onClick={() => onOpenDelegation(node.execution!.delegation_id)} title="打开委派详情">delegation {node.execution.delegation_id} <ChevronRight size={12} /></button>}{delegation?.report?.error_message && <div className="coordinator-node-error">{delegation.report.error_message}</div>}{node.intent.acceptance_criteria.length > 0 && <ul>{node.intent.acceptance_criteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul>}{(canAccept || canReconcile || canRetry) && <div className="coordinator-node-actions">{canReconcile && <button type="button" className="warning-button" onClick={() => setReconcileOpen((value) => !value)} disabled={pending}>对账</button>}{canAccept && <button type="button" className="primary-button" onClick={() => void run(() => api.coordinatorNodeAccept(session.session_id, node.node_id, session.revision))} disabled={pending}>验收通过</button>}{canRetry && <button type="button" className="secondary-button" onClick={() => void run(() => api.coordinatorNodeRetry(session.session_id, node.node_id))} disabled={pending}>重试</button>}</div>}{canSupplement && <div className="coordinator-node-supplement"><textarea value={supplement} onChange={(event) => setSupplement(event.target.value)} placeholder="要求 Agent 补充说明或证据…" rows={2} disabled={pending} /><button type="button" className="secondary-button" onClick={() => void sendSupplement()} disabled={pending || !supplement.trim()}>要求补充</button></div>}{reconcileOpen && <div className="coordinator-reconcile-form"><label>对账结论<select value={reconcileStatus} onChange={(event) => setReconcileStatus(event.target.value as typeof reconcileStatus)}><option value="failed">确认失败</option><option value="completed">确认已完成</option><option value="cancelled">确认已取消</option></select></label><label>对账依据<textarea value={reconcileReason} onChange={(event) => setReconcileReason(event.target.value)} placeholder="例如：Codex 会话创建超时且没有任何输出，无法证明成功。" rows={3} disabled={pending} /></label><div className="coordinator-node-actions"><button type="button" className="secondary-button" onClick={() => setReconcileOpen(false)} disabled={pending}>取消</button><button type="button" className="warning-button" onClick={() => void reconcile()} disabled={pending || !reconcileReason.trim()}>提交对账</button></div></div>}{error && <div className="coordinator-node-error">{error}</div>}</article>
}

function ApprovalCard({ approval, session, onResolved }: { approval: Record<string, unknown>; session: CoordinatorSessionDomain; onResolved: () => void }) {
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
  return <div className="coordinator-approval-card"><strong>{String(approval.reason ?? '需要人工审批')}</strong><small>{String(approval.action_key ?? approval.approval_id)}</small><div><button className="secondary-button" onClick={() => void resolve(false)} disabled={pending}>拒绝</button><button className="primary-button" onClick={() => void resolve(true)} disabled={pending}>批准</button></div>{error && <div className="coordinator-node-error">{error}</div>}</div>
}

function CoordinatorComposer({ onClose, onCreated }: { onClose: () => void; onCreated: (sessionId: string) => void }) {
  const [sessionId, setSessionId] = useState('coordinator-' + Date.now().toString(36))
  const [cwd, setCwd] = useState('')
  const [prompt, setPrompt] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string>()
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const normalizedSessionId = sessionId.trim()
    const normalizedCwd = cwd.trim()
    const normalizedPrompt = prompt.trim()
    if (!normalizedSessionId || !normalizedCwd || !normalizedPrompt || pending) return
    setPending(true)
    setError(undefined)
    try {
      const activation = await api.createCoordinatorSession({
        session_id: normalizedSessionId,
        cwd: normalizedCwd,
        prompt: normalizedPrompt,
        activation_id: 'web-' + crypto.randomUUID(),
      })
      if (activation.session.session_id !== normalizedSessionId) {
        throw new Error('Coordinator 首次激活返回的会话标识不一致。')
      }
      onCreated(normalizedSessionId)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setPending(false)
    }
  }
  const pendingLabel = '正在等待模型首次响应…'
  return <div className="modal-backdrop"><form className="modal coordinator-composer-modal" onSubmit={submit}><div className="modal-header"><div><span className="eyebrow">NEW COORDINATOR SESSION</span><h2>创建持续会话</h2></div><button type="button" className="icon-button" onClick={onClose} disabled={pending} title={pending ? pendingLabel : '关闭'}><X size={16} /></button></div><label>会话 ID<input value={sessionId} onChange={(event) => setSessionId(event.target.value)} disabled={pending} /></label><label>工作目录<input value={cwd} onChange={(event) => setCwd(event.target.value)} placeholder="例如 D:\\dev\\AITools\\multi-agent-v3" disabled={pending} /></label><label>目标<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={5} placeholder="描述需要持续推进的复杂目标…" disabled={pending} /></label>{pending && <div className="warning-banner"><LoaderCircle size={14} className="spin" />{pendingLabel}<span>模型首次返回前不会关闭弹窗或新增会话条目。</span></div>}{error && <div className="error-banner">创建失败：{error}</div>}<div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose} disabled={pending}>取消</button><button type="submit" className="primary-button" disabled={pending || !cwd.trim() || !prompt.trim()}>{pending ? <><LoaderCircle size={15} className="spin" />{pendingLabel}</> : <><Plus size={15} />创建并激活</>}</button></div></form></div>
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
  return '未选择会话'
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
