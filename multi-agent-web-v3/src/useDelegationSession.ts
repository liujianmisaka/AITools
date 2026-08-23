import { useEffect, useRef, useState } from 'react'
import { api, delegationSessionStreamUrl } from './api'
import type { Delegation, DelegationSession, DelegationSessionEvent } from './types'

export type DelegationSessionConnectionState =
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'ended'

type SessionOutput = {
  text: string
  terminalOutput: unknown
}

type DelegationSessionPayload = {
  session: DelegationSession | null
  events: DelegationSessionEvent[]
  outputText: string
  terminalOutput: unknown
  connection: DelegationSessionConnectionState
  lastSequence: number
  error?: string
}

type SessionSnapshotHandler = (snapshot: Delegation) => void

const MAX_DISPLAY_EVENTS = 500
const MAX_OUTPUT_CHARS = 24_000

export function useDelegationSession(
  delegationId: string,
  onSnapshot?: SessionSnapshotHandler,
): DelegationSessionPayload {
  const [session, setSession] = useState<DelegationSession | null>(null)
  const [events, setEvents] = useState<DelegationSessionEvent[]>([])
  const [outputText, setOutputText] = useState('')
  const [terminalOutput, setTerminalOutput] = useState<unknown>(null)
  const [connection, setConnection] = useState<DelegationSessionConnectionState>('connecting')
  const [error, setError] = useState<string>()
  const snapshotHandler = useRef(onSnapshot)

  useEffect(() => {
    snapshotHandler.current = onSnapshot
  }, [onSnapshot])

  useEffect(() => {
    let disposed = false
    let source: EventSource | null = null
    let reconnectTimer: number | undefined
    let lastSequence = 0
    let connectionState: DelegationSessionConnectionState = 'connecting'
    // Keep the full ordered history for output reconstruction; only the rendered list is capped.
    const eventHistoryRef: { current: DelegationSessionEvent[] } = { current: [] }

    const setConnectionState = (next: DelegationSessionConnectionState) => {
      connectionState = next
      if (!disposed) setConnection(next)
    }

    const updateOutput = (nextEvents: DelegationSessionEvent[]) => {
      const output = deriveOutput(nextEvents)
      if (!disposed) {
        setOutputText(output.text)
        setTerminalOutput(output.terminalOutput)
      }
    }

    const updateEvents = (next: DelegationSessionEvent[]) => {
      const bySequence = new Map<number, DelegationSessionEvent>()
      for (const event of next) bySequence.set(event.sequence, event)
      const ordered = [...bySequence.values()].sort(
        (left, right) => left.sequence - right.sequence,
      )
      lastSequence = ordered.reduce(
        (highest, event) => Math.max(highest, event.sequence),
        lastSequence,
      )
      eventHistoryRef.current = ordered
      const visible = ordered.slice(-MAX_DISPLAY_EVENTS)
      if (!disposed) {
        setEvents(visible)
        updateOutput(ordered)
      }
    }

    const mergeEvent = (event: DelegationSessionEvent) => {
      const current = eventHistoryRef.current
      const index = current.findIndex((item) => item.sequence === event.sequence)
      if (index < 0) {
        updateEvents([...current, event])
      } else {
        const next = [...current]
        next[index] = event
        updateEvents(next)
      }
      setError(undefined)
      setConnectionState('connected')
    }

    const applySession = (next: DelegationSession) => {
      if (disposed) return
      setSession(next)
      snapshotHandler.current?.(next.delegation)
      lastSequence = Math.max(lastSequence, next.last_sequence)
      setError(undefined)
    }

    const refresh = async (includeEvents: boolean) => {
      try {
        const sessionRequest = api.delegationSession(delegationId)
        const eventsRequest = includeEvents
          ? api.delegationSessionEvents(delegationId, lastSequence + 1)
          : null
        const [nextSession, nextEvents] = await Promise.all([
          sessionRequest,
          eventsRequest ?? Promise.resolve(null),
        ])
        if (disposed) return
        applySession(nextSession)
        if (nextEvents !== null && nextEvents.length > 0) {
          updateEvents([...eventHistoryRef.current, ...nextEvents])
        }
      } catch (reason) {
        if (!disposed) {
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      }
    }

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== undefined || connectionState === 'ended') return
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined
        if (disposed || connectionState === 'ended') return
        openStream(lastSequence + 1)
      }, 3_000)
    }

    const openStream = (startSequence: number) => {
      if (disposed || connectionState === 'ended') return
      source?.close()
      setConnectionState('connecting')
      const nextSource = new EventSource(
        delegationSessionStreamUrl(delegationId, startSequence),
      )
      source = nextSource
      nextSource.onopen = () => {
        setConnectionState('connected')
        setError(undefined)
      }
      nextSource.onerror = () => {
        if (disposed || connectionState === 'ended') return
        setConnectionState('reconnecting')
        if (nextSource.readyState === EventSource.CLOSED) scheduleReconnect()
      }
      nextSource.addEventListener('delegation.session.event', (event) => {
        try {
          mergeEvent(JSON.parse((event as MessageEvent<string>).data) as DelegationSessionEvent)
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : '会话事件格式无效')
        }
      })
      nextSource.addEventListener('delegation.session.snapshot', (event) => {
        try {
          const snapshot = JSON.parse((event as MessageEvent<string>).data) as DelegationSession
          applySession(snapshot)
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : '会话状态格式无效')
        }
      })
      nextSource.addEventListener('delegation.session.end', () => {
        setConnectionState('ended')
        nextSource.close()
        void refresh(true)
      })
    }

    const initialize = async () => {
      await refresh(true)
      if (!disposed) openStream(1)
    }

    void initialize()
    const fallbackTimer = window.setInterval(() => {
      if (disposed || connectionState === 'ended') return
      void refresh(connectionState !== 'connected')
      if (connectionState !== 'connected') scheduleReconnect()
    }, 5_000)

    return () => {
      disposed = true
      source?.close()
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer)
      window.clearInterval(fallbackTimer)
    }
  }, [delegationId])

  return {
    session,
    events,
    outputText,
    terminalOutput,
    connection,
    lastSequence: events.reduce(
      (highest, event) => Math.max(highest, event.sequence),
      session?.last_sequence ?? 0,
    ),
    error,
  }
}

function deriveOutput(events: DelegationSessionEvent[]): SessionOutput {
  let currentActivation: number | null = null
  let deltaText = ''
  let completedText: string | null = null
  let terminalOutput: unknown = null

  for (const event of events) {
    if (
      event.activation_number !== null &&
      event.activation_number !== currentActivation
    ) {
      currentActivation = event.activation_number
      deltaText = ''
      completedText = null
      terminalOutput = null
    }
    if (event.kind === 'output_delta') {
      const text = stringPayload(event.payload.text)
      if (text) deltaText += text
    } else if (event.kind === 'output_completed') {
      const text = stringPayload(event.payload.text)
      if (text) completedText = text
    } else if (event.kind === 'terminal') {
      terminalOutput = event.payload.output
      if (typeof terminalOutput === 'string') completedText = terminalOutput
    }
  }

  const text = (completedText ?? deltaText).slice(-MAX_OUTPUT_CHARS)
  return { text, terminalOutput }
}

function stringPayload(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}
