import { useEffect, useRef, useState } from 'react'
import { api, delegationEventsStreamUrl } from './api'
import type { Delegation, InteractionMessage } from './types'

export type DelegationConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'ended'

type DelegationEventPayload = {
  messages: InteractionMessage[]
  connection: DelegationConnectionState
  lastSequence: number
  error?: string
}

type StreamSnapshotHandler = (snapshot: Delegation) => void
const MAX_DISPLAY_EVENTS = 200

export function useDelegationEvents(
  delegationId: string,
  onSnapshot?: StreamSnapshotHandler,
): DelegationEventPayload {
  const [messages, setMessages] = useState<InteractionMessage[]>([])
  const [connection, setConnection] = useState<DelegationConnectionState>('connecting')
  const [error, setError] = useState<string>()
  const snapshotHandler = useRef(onSnapshot)
  const messagesRef = useRef<InteractionMessage[]>([])

  useEffect(() => {
    snapshotHandler.current = onSnapshot
  }, [onSnapshot])

  useEffect(() => {
    let disposed = false
    let source: EventSource | null = null
    let connectionState: DelegationConnectionState = 'connecting'
    let lastSnapshotAt = 0
    let lastSequence = 0

    const updateMessages = (next: InteractionMessage[]) => {
      if (disposed) return
      const ordered = [...next].sort((left, right) => left.sequence - right.sequence)
      lastSequence = ordered.reduce(
        (highest, message) => Math.max(highest, message.sequence),
        0,
      )
      const visible = ordered.slice(-MAX_DISPLAY_EVENTS)
      messagesRef.current = visible
      setMessages(visible)
    }

    const mergeMessage = (message: InteractionMessage) => {
      const current = messagesRef.current
      const index = current.findIndex(
        (item) => item.message_id === message.message_id || item.sequence === message.sequence,
      )
      if (index < 0) {
        updateMessages([...current, message])
        return
      }
      const next = [...current]
      next[index] = message
      updateMessages(next)
    }

    const refresh = async (includeHistory: boolean) => {
      try {
        const snapshotRequest = api.delegation(delegationId)
        const historyRequest = includeHistory ? api.delegationEvents(delegationId) : null
        const [snapshot, history] = await Promise.all([
          snapshotRequest,
          historyRequest ?? Promise.resolve(null),
        ])
        if (disposed) return
        snapshotHandler.current?.(snapshot)
        lastSnapshotAt = Date.now()
        setError(undefined)
        if (history !== null) updateMessages(history)
      } catch (reason) {
        if (disposed) return
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    }

    const setConnectionState = (next: DelegationConnectionState) => {
      connectionState = next
      if (!disposed) setConnection(next)
    }

    const handleMessage = (event: MessageEvent<string>) => {
      try {
        mergeMessage(JSON.parse(event.data) as InteractionMessage)
        setError(undefined)
        setConnectionState('connected')
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : '事件数据格式无效')
      }
    }

    const handleSnapshot = (event: MessageEvent<string>) => {
      try {
        const snapshot = JSON.parse(event.data) as Delegation
        snapshotHandler.current?.(snapshot)
        lastSnapshotAt = Date.now()
        setError(undefined)
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : '状态数据格式无效')
      }
    }

    const handleEnd = () => {
      setConnectionState('ended')
      source?.close()
      void refresh(true)
    }

    const openStream = (startSequence: number) => {
      source = new EventSource(delegationEventsStreamUrl(delegationId, startSequence))
      source.onopen = () => {
        setConnectionState('connected')
        setError(undefined)
      }
      source.onerror = () => {
        if (!disposed && connectionState !== 'ended') setConnectionState('reconnecting')
      }
      source.addEventListener('delegation.message', (event) =>
        handleMessage(event as MessageEvent<string>),
      )
      source.addEventListener('delegation.snapshot', (event) =>
        handleSnapshot(event as MessageEvent<string>),
      )
      source.addEventListener('delegation.end', handleEnd)
    }

    const initialize = async () => {
      await refresh(true)
      if (disposed) return
      openStream(lastSequence + 1)
    }

    void initialize()
    const fallbackTimer = window.setInterval(() => {
      if (disposed || connectionState === 'ended') return
      const interval = connectionState === 'connected' ? 15_000 : 3_000
      if (Date.now() - lastSnapshotAt < interval) return
      void refresh(true)
    }, 1_000)

    return () => {
      disposed = true
      source?.close()
      window.clearInterval(fallbackTimer)
    }
  }, [delegationId])

  return {
    messages,
    connection,
    lastSequence: messages.reduce(
      (highest, message) => Math.max(highest, message.sequence),
      0,
    ),
    error,
  }
}
