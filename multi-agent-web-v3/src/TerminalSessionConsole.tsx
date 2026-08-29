import { FitAddon } from '@xterm/addon-fit'
import { Terminal as XTerm } from '@xterm/xterm'
import { Eye, Keyboard, LoaderCircle, RotateCw } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, terminalSessionStreamUrl } from './api'
import type {
  TerminalHostAccess,
  TerminalRuntime,
  TerminalServerMessage,
  TerminalSession,
} from './types'

type TerminalSessionConsoleProps = {
  delegationId: string
  providerId: string | null
  providerSessionId: string | null
  cwd: string | null
  archived: boolean
}

type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'ended'

const DEFAULT_COLUMNS = 120
const DEFAULT_ROWS = 34
const ACTIVE_TERMINAL_STATUSES = new Set<TerminalSession['status']>(['starting', 'running'])

export function TerminalSessionConsole({
  delegationId,
  providerId,
  providerSessionId,
  cwd,
  archived,
}: TerminalSessionConsoleProps) {
  const terminalElementRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<XTerm | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const terminalSessionRef = useRef<TerminalSession | null>(null)
  const leaseTokenRef = useRef<string | null>(null)
  const clientIdRef = useRef(crypto.randomUUID())
  const [access, setAccess] = useState<TerminalHostAccess | null>(null)
  const [terminalSession, setTerminalSession] = useState<TerminalSession | null>(null)
  const [runtime, setRuntime] = useState<TerminalRuntime | null>(null)
  const [connection, setConnection] = useState<ConnectionState>('idle')
  const [leaseToken, setLeaseToken] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [unavailableReason, setUnavailableReason] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const updateTerminalSession = (next: TerminalSession) => {
    terminalSessionRef.current = next
    setTerminalSession(next)
    if (
      next.input_lease === null ||
      next.input_lease.client_id !== clientIdRef.current
    ) {
      updateLease(null)
    }
  }

  const updateLease = (next: string | null) => {
    leaseTokenRef.current = next
    setLeaseToken(next)
    if (terminalRef.current !== null) {
      terminalRef.current.options.disableStdin = next === null
    }
  }

  useLayoutEffect(() => {
    const container = terminalElementRef.current
    if (container === null) return
    const terminal = new XTerm({
      allowProposedApi: false,
      cols: DEFAULT_COLUMNS,
      rows: DEFAULT_ROWS,
      cursorBlink: true,
      disableStdin: true,
      fontFamily: 'Cascadia Mono, Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.18,
      scrollback: 5_000,
      theme: {
        background: '#091019',
        foreground: '#d8e2ee',
        cursor: '#7dd3fc',
        selectionBackground: '#1e4f6f',
      },
    })
    const fitAddon = new FitAddon()
    terminal.loadAddon(fitAddon)
    terminal.open(container)
    terminalRef.current = terminal
    fitAddonRef.current = fitAddon

    const fit = () => {
      try {
        fitAddon.fit()
      } catch {
        // The host element may be between layouts while the detail pane is resized.
      }
    }
    const frame = window.requestAnimationFrame(fit)
    const resizeObserver = new ResizeObserver(fit)
    resizeObserver.observe(container)
    const input = terminal.onData((data) => {
      const socket = socketRef.current
      const token = leaseTokenRef.current
      if (socket?.readyState !== WebSocket.OPEN || token === null) return
      socket.send(JSON.stringify({ type: 'input', lease_token: token, data }))
    })
    const resize = terminal.onResize(({ cols, rows }) => {
      const socket = socketRef.current
      const token = leaseTokenRef.current
      if (socket?.readyState !== WebSocket.OPEN || token === null) return
      socket.send(JSON.stringify({ type: 'resize', lease_token: token, cols, rows }))
    })

    return () => {
      window.cancelAnimationFrame(frame)
      resizeObserver.disconnect()
      input.dispose()
      resize.dispose()
      terminal.dispose()
      terminalRef.current = null
      fitAddonRef.current = null
    }
  }, [])

  useEffect(() => {
    let disposed = false
    setAccess(null)
    setRuntime(null)
    terminalSessionRef.current = null
    setTerminalSession(null)
    updateLease(null)
    setConnection('idle')
    setError(null)
    setUnavailableReason(null)

    if (providerId === null || providerSessionId === null || cwd === null) {
      setUnavailableReason(
        providerSessionId === null
          ? '等待 Provider 会话绑定后即可打开交互终端。'
          : '该委派没有可信工作目录，无法启动终端。',
      )
      return () => {
        disposed = true
      }
    }

    const initialize = async () => {
      setLoading(true)
      try {
        const nextAccess = await api.terminalHostAccess()
        const provider = nextAccess.providers.find((item) => item.provider_id === providerId)
        if (provider === undefined) {
          throw new Error(`管理配置中没有 Provider：${providerId}`)
        }
        if (provider.kind === 'fake') {
          if (!disposed) setUnavailableReason('Fake Provider 不提供交互终端。')
          return
        }
        const nextRuntime = provider.kind
        const existing = selectTerminalSession(
          await api.terminalSessions(delegationId, nextAccess.token),
          providerId,
          providerSessionId,
          nextRuntime,
        )
        const nextSession =
          existing ??
          (archived
            ? null
            : await api.createTerminalSession(
                {
                  delegation_id: delegationId,
                  provider_id: providerId,
                  provider_session_id: providerSessionId,
                  runtime: nextRuntime,
                  cwd,
                  cols: terminalRef.current?.cols ?? DEFAULT_COLUMNS,
                  rows: terminalRef.current?.rows ?? DEFAULT_ROWS,
                },
                nextAccess.token,
              ))
        if (disposed) return
        setAccess(nextAccess)
        setRuntime(nextRuntime)
        if (nextSession === null) {
          setUnavailableReason('该历史委派没有可回放的 Terminal Host 会话。')
          return
        }
        updateTerminalSession(nextSession)
      } catch (reason) {
        if (!disposed) {
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      } finally {
        if (!disposed) setLoading(false)
      }
    }

    void initialize()
    return () => {
      disposed = true
    }

  }, [archived, cwd, delegationId, providerId, providerSessionId, reloadToken])

  useEffect(() => {
    if (access === null || terminalSession === null) return
    let disposed = false
    let reconnectTimer: number | undefined

    const connect = () => {
      if (disposed) return
      setConnection((current) => (current === 'idle' ? 'connecting' : 'reconnecting'))
      const socket = new WebSocket(
        terminalSessionStreamUrl(terminalSession.id, clientIdRef.current),
        ['aitools-terminal.v1', `aitools-terminal-token.${access.token}`],
      )
      socketRef.current = socket
      socket.onopen = () => {
        if (disposed) return
        setConnection('connected')
        setError(null)
      }
      socket.onmessage = (event) => {
        try {
          applyServerMessage(JSON.parse(String(event.data)) as TerminalServerMessage)
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : '终端消息格式无效')
        }
      }
      socket.onerror = () => {
        if (!disposed) setError('Terminal Host WebSocket 连接发生错误。')
      }
      socket.onclose = () => {
        if (socketRef.current === socket) socketRef.current = null
        updateLease(null)
        if (disposed) return
        if (ACTIVE_TERMINAL_STATUSES.has(terminalSessionRef.current?.status ?? 'failed')) {
          setConnection('reconnecting')
          reconnectTimer = window.setTimeout(connect, 3_000)
        } else {
          setConnection('ended')
        }
      }
    }

    const applyServerMessage = (message: TerminalServerMessage) => {
      if (message.type === 'snapshot') {
        updateTerminalSession(message.session)
        terminalRef.current?.reset()
        terminalRef.current?.write(message.data)
        fitAddonRef.current?.fit()
      } else if (message.type === 'output') {
        terminalRef.current?.write(message.data)
      } else if (message.type === 'session.updated') {
        updateTerminalSession(message.session)
      } else if (message.type === 'lease.granted') {
        updateLease(message.lease_token)
        const terminal = terminalRef.current
        const socket = socketRef.current
        if (terminal !== null && socket?.readyState === WebSocket.OPEN) {
          socket.send(
            JSON.stringify({
              type: 'resize',
              lease_token: message.lease_token,
              cols: terminal.cols,
              rows: terminal.rows,
            }),
          )
          terminal.focus()
        }
      } else if (message.type === 'lease.released') {
        updateLease(null)
      } else {
        setError(`${message.code}: ${message.message}`)
        if (message.code === 'terminal.input_lease_required') updateLease(null)
      }
    }

    connect()
    return () => {
      disposed = true
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer)
      const socket = socketRef.current
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'detach' }))
      }
      socket?.close()
      socketRef.current = null
      updateLease(null)
    }
  }, [access, terminalSession?.id])

  useEffect(() => {
    if (leaseToken === null) return
    const timer = window.setInterval(() => {
      const socket = socketRef.current
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'lease.renew', lease_token: leaseToken }))
      }
    }, 15_000)
    return () => window.clearInterval(timer)
  }, [leaseToken])

  const sendLeaseAction = (type: 'lease.acquire' | 'lease.release') => {
    const socket = socketRef.current
    if (socket?.readyState !== WebSocket.OPEN) return
    if (type === 'lease.acquire') {
      socket.send(JSON.stringify({ type }))
    } else if (leaseToken !== null) {
      socket.send(JSON.stringify({ type, lease_token: leaseToken }))
    }
  }

  const controlledByAnotherClient =
    terminalSession?.input_lease !== null &&
    terminalSession?.input_lease !== undefined &&
    terminalSession.input_lease.client_id !== clientIdRef.current
  const inputAvailable =
    terminalSession?.status === 'running' && connection === 'connected' && !archived

  return (
    <section className="terminal-session-console">
      <div className="terminal-session-header">
        <div>
          <div className="result-title">Agent 交互终端</div>
          <strong>
            {runtime === null ? '等待终端会话' : `${runtime.toUpperCase()} TUI`}
          </strong>
        </div>
        <div className="terminal-session-actions">
          <span className={`terminal-connection ${connection}`}>
            {connectionLabel(connection)}
          </span>
          {leaseToken === null ? (
            <button
              className="secondary-button terminal-control-button"
              type="button"
              disabled={!inputAvailable || controlledByAnotherClient}
              onClick={() => sendLeaseAction('lease.acquire')}
            >
              <Keyboard size={14} />
              {controlledByAnotherClient ? '其他窗口控制中' : '接管输入'}
            </button>
          ) : (
            <button
              className="secondary-button terminal-control-button active"
              type="button"
              onClick={() => sendLeaseAction('lease.release')}
            >
              <Eye size={14} />
              切换只读
            </button>
          )}
        </div>
      </div>
      <div className="terminal-session-surface-wrap">
        <div className="terminal-session-surface" ref={terminalElementRef} />
        {(loading || unavailableReason !== null) && (
          <div className="terminal-session-overlay">
            {loading ? <LoaderCircle className="spin" size={20} /> : <Eye size={20} />}
            <span>{loading ? '正在创建或恢复终端会话…' : unavailableReason}</span>
          </div>
        )}
      </div>
      {error && (
        <div className="error-banner terminal-session-error">
          <span>{error}</span>
          <button
            className="secondary-button terminal-retry-button"
            type="button"
            onClick={() => setReloadToken((current) => current + 1)}
          >
            <RotateCw size={13} />
            重试
          </button>
        </div>
      )}
      <div className="terminal-session-footer">
        <span>
          状态 {terminalSession?.status ?? '—'} · 输出 #{terminalSession?.sequence ?? 0}
        </span>
        <span>{leaseToken === null ? '只读观察' : '已取得输入控制'}</span>
        <span title={cwd ?? undefined}>{cwd ?? '等待工作目录'}</span>
      </div>
    </section>
  )
}

function selectTerminalSession(
  sessions: TerminalSession[],
  providerId: string,
  providerSessionId: string,
  runtime: TerminalRuntime,
): TerminalSession | null {
  const matching = sessions
    .filter(
      (session) =>
        session.provider_id === providerId &&
        session.provider_session_id === providerSessionId &&
        session.runtime === runtime,
    )
    .sort((left, right) => right.created_at.localeCompare(left.created_at))
  return matching.find((session) => ACTIVE_TERMINAL_STATUSES.has(session.status)) ?? matching[0] ?? null
}

function connectionLabel(state: ConnectionState): string {
  if (state === 'connected') return '终端已连接'
  if (state === 'connecting') return '终端连接中'
  if (state === 'reconnecting') return '终端重连中'
  if (state === 'ended') return '终端已结束'
  return '终端未连接'
}

export default TerminalSessionConsole
