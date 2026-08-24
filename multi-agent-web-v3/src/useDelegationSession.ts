import { useEffect, useRef, useState } from 'react'
import { api, delegationSessionStreamUrl } from './api'
import {
  buildSessionTimeline,
  isSessionDeltaEvent,
  type AgentSessionTurn,
} from './sessionTimeline'
import type { Delegation, DelegationSession, DelegationSessionEvent } from './types'

export type DelegationSessionConnectionState =
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'ended'

type DelegationSessionPayload = {
  session: DelegationSession | null
  events: DelegationSessionEvent[]
  timeline: AgentSessionTurn[]
  terminalOutput: unknown
  connection: DelegationSessionConnectionState
  lastSequence: number
  error?: string
}

type SessionSnapshotHandler = (snapshot: Delegation) => void

const MAX_DISPLAY_EVENTS = 500
const MAX_TIMELINE_EVENTS = 5_000
const DELTA_RENDER_INTERVAL_MS = 100

export function useDelegationSession(
  delegationId: string,
  onSnapshot?: SessionSnapshotHandler,
): DelegationSessionPayload {
  const [session, setSession] = useState<DelegationSession | null>(null)
  const [events, setEvents] = useState<DelegationSessionEvent[]>([])
  const [timeline, setTimeline] = useState<AgentSessionTurn[]>([])
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
    let deltaRenderTimer: number | undefined
    let pendingDeltaEvents: DelegationSessionEvent[] = []
    let lastSequence = 0
    let connectionState: DelegationSessionConnectionState = 'connecting'
    // Keep the full ordered history for output reconstruction; only the rendered list is capped.
    const eventHistoryRef: { current: DelegationSessionEvent[] } = { current: [] }

    const setConnectionState = (next: DelegationSessionConnectionState) => {
      connectionState = next
      if (!disposed) setConnection(next)
    }

    const updateTerminalOutput = (nextEvents: DelegationSessionEvent[]) => {
      if (!disposed) setTerminalOutput(deriveTerminalOutput(nextEvents))
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
      const retained = ordered.slice(-MAX_TIMELINE_EVENTS)
      eventHistoryRef.current = retained
      const visible = retained.slice(-MAX_DISPLAY_EVENTS)
      if (!disposed) {
        setEvents(visible)
        setTimeline(buildSessionTimeline(retained))
        updateTerminalOutput(retained)
      }
    }

    const mergeEvents = (incoming: DelegationSessionEvent[]) => {
      updateEvents([...eventHistoryRef.current, ...incoming])
      setError(undefined)
      setConnectionState('connected')
    }

    const flushPendingDeltas = () => {
      if (deltaRenderTimer !== undefined) {
        window.clearTimeout(deltaRenderTimer)
        deltaRenderTimer = undefined
      }
      if (pendingDeltaEvents.length === 0) return
      const pending = pendingDeltaEvents
      pendingDeltaEvents = []
      mergeEvents(pending)
    }

    const queueEvent = (event: DelegationSessionEvent) => {
      if (!isSessionDeltaEvent(event.kind)) {
        flushPendingDeltas()
        mergeEvents([event])
        return
      }
      pendingDeltaEvents.push(event)
      if (deltaRenderTimer !== undefined) return
      deltaRenderTimer = window.setTimeout(flushPendingDeltas, DELTA_RENDER_INTERVAL_MS)
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
          queueEvent(JSON.parse((event as MessageEvent<string>).data) as DelegationSessionEvent)
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
        flushPendingDeltas()
        setConnectionState('ended')
        nextSource.close()
        void refresh(true)
      })
    }

    const initialize = async () => {
      await refresh(true)
      if (!disposed) openStream(lastSequence + 1)
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
      if (deltaRenderTimer !== undefined) window.clearTimeout(deltaRenderTimer)
      window.clearInterval(fallbackTimer)
    }
  }, [delegationId])

  return {
    session,
    events,
    timeline,
    terminalOutput,
    connection,
    lastSequence: events.reduce(
      (highest, event) => Math.max(highest, event.sequence),
      session?.last_sequence ?? 0,
    ),
    error,
  }
}

function deriveTerminalOutput(events: DelegationSessionEvent[]): unknown {
  let currentActivation: number | null = null
  let terminalOutput: unknown = null

  for (const event of events) {
    if (
      event.activation_number !== null &&
      event.activation_number !== currentActivation
    ) {
      currentActivation = event.activation_number
      terminalOutput = null
    }
    if (event.kind === 'terminal') {
      terminalOutput = event.payload.output
    }
  }
  return terminalOutput
}
