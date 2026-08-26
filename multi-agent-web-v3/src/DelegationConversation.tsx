import { useMemo, useState } from 'react'
import {
  Bot,
  CornerDownLeft,
  LoaderCircle,
  MessageCircleQuestion,
  Send,
  UserRound,
} from 'lucide-react'
import { api, delegationActor } from './api'
import { MarkdownContent } from './MarkdownContent'
import type { AgentSessionTurn } from './sessionTimeline'
import type {
  Delegation,
  DelegationSession,
  InteractionMessage,
  MessageDispatch,
  MessageDispatchSubmission,
} from './types'

type ConversationRole = 'controller' | 'agent'

type ConversationEntry = {
  key: string
  role: ConversationRole
  kind: 'instruction' | 'answer' | 'question' | 'output'
  text: string
  timestamp: string
  sequence: number
  activationNumber?: number
  deliveryStatus?: string
  messageId?: string
  correlationId?: string
  answered: boolean
  options: string[]
}

type DelegationConversationProps = {
  delegation: Delegation
  session: DelegationSession | null
  messages: InteractionMessage[]
  timeline: AgentSessionTurn[]
  onDispatched: (dispatch: MessageDispatch) => Promise<void> | void
}

export function DelegationConversation({
  delegation,
  session,
  messages,
  timeline,
  onDispatched,
}: DelegationConversationProps) {
  const entries = useMemo(
    () => buildConversationEntries(messages, timeline),
    [messages, timeline],
  )
  const [content, setContent] = useState('')
  const [delivery, setDelivery] = useState<'append' | 'interrupt_continue'>('append')
  const [model, setModel] = useState('')
  const [effort, setEffort] = useState('')
  const [replyTarget, setReplyTarget] = useState<ConversationEntry | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastDispatch, setLastDispatch] = useState<MessageDispatch | null>(null)
  const sessionId = session?.delegation.session_id ?? delegation.session_id
  const activeActivationId = delegation.current_activation_id
  const sessionClosed = session?.closed === true

  const chooseReply = (entry: ConversationEntry, option?: string) => {
    setReplyTarget(entry)
    setDelivery('append')
    if (option !== undefined) setContent(option)
  }

  const submit = async () => {
    const text = content.trim()
    if (!text) {
      setError('请输入要发送给 Agent 的消息。')
      return
    }
    if (sessionId === null) {
      setError('当前委派还没有可继续的 Session。')
      return
    }
    if (sessionClosed) {
      setError('该 Session 已关闭，不能继续发送消息。')
      return
    }
    if (delivery === 'interrupt_continue' && activeActivationId === null) {
      setError('interrupt_continue 只能用于仍在执行的 Activation。')
      return
    }
    const normalizedModel = model.trim()
    const normalizedEffort = effort.trim()
    if ((normalizedModel.length === 0) !== (normalizedEffort.length === 0)) {
      setError('覆盖执行选择时必须同时填写模型和 effort。')
      return
    }

    const dispatchId = 'web-dispatch-' + crypto.randomUUID()
    const payload: MessageDispatchSubmission = {
      dispatch_id: dispatchId,
      idempotency_key: dispatchId,
      actor: {
        principal_id: delegationActor.actorId,
        kind: delegationActor.actorKind,
      },
      session_id: sessionId,
      delivery: replyTarget === null ? delivery : 'append',
      message_id: 'web-message-' + crypto.randomUUID(),
      message_type: replyTarget === null ? 'instruction' : 'answer',
      payload: { prompt: text },
    }
    if (activeActivationId !== null) payload.expected_activation_id = activeActivationId
    if (replyTarget?.messageId && replyTarget.correlationId) {
      payload.reply_to = replyTarget.messageId
      payload.correlation_id = replyTarget.correlationId
    }
    if (normalizedModel && normalizedEffort) {
      payload.model = normalizedModel
      payload.effort = normalizedEffort
    }

    setSubmitting(true)
    setError(null)
    try {
      const dispatch = await api.dispatchDelegationMessage(delegation.delegation_id, payload)
      setLastDispatch(dispatch)
      if (dispatch.status === 'rejected' || dispatch.status === 'reconciliation_required') {
        setError(dispatch.error_message ?? '消息没有得到确定投递结果。')
      } else {
        setContent('')
        setReplyTarget(null)
      }
      await onDispatched(dispatch)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="delegation-conversation">
      <div className="conversation-header">
        <div>
          <div className="result-title">委派会话</div>
          <strong>委托者与 Agent 的消息和实时回答</strong>
        </div>
        <span>{entries.length} 条对话消息</span>
      </div>

      <div className="conversation-transcript">
        {entries.length === 0 ? (
          <div className="timeline-empty">等待委托者消息或 Agent 回答…</div>
        ) : (
          entries.map((entry) => (
            <ConversationBubble
              entry={entry}
              key={entry.key}
              replyable={!sessionClosed && sessionId !== null}
              onReply={chooseReply}
            />
          ))
        )}
      </div>

      <div className="conversation-composer">
        {replyTarget !== null && (
          <div className="reply-target">
            <MessageCircleQuestion size={14} />
            <span>正在回答：{replyTarget.text}</span>
            <button type="button" onClick={() => setReplyTarget(null)}>取消</button>
          </div>
        )}
        <textarea
          rows={4}
          value={content}
          onChange={(event) => setContent(event.target.value)}
          placeholder={
            sessionClosed
              ? 'Session 已关闭'
              : activeActivationId
                ? '向正在执行的 Agent 追加消息…'
                : '在同一 Agent Session 中继续下一轮…'
          }
          disabled={sessionClosed || sessionId === null}
        />
        <div className="composer-controls">
          <label>
            投递方式
            <select
              value={replyTarget === null ? delivery : 'append'}
              onChange={(event) =>
                setDelivery(event.target.value as 'append' | 'interrupt_continue')
              }
              disabled={replyTarget !== null}
            >
              <option value="append">追加消息</option>
              <option value="interrupt_continue" disabled={activeActivationId === null}>
                打断并继续
              </option>
            </select>
          </label>
          <details className="composer-advanced">
            <summary>下一 Activation 执行选择</summary>
            <div>
              <label>
                模型
                <input value={model} onChange={(event) => setModel(event.target.value)} />
              </label>
              <label>
                Effort
                <input value={effort} onChange={(event) => setEffort(event.target.value)} />
              </label>
            </div>
          </details>
          <button
            className="primary-button conversation-send"
            type="button"
            onClick={() => void submit()}
            disabled={submitting || sessionClosed || sessionId === null}
          >
            {submitting ? <LoaderCircle className="spin" size={15} /> : <Send size={15} />}
            {submitting ? '发送中' : replyTarget === null ? '发送消息' : '发送回答'}
          </button>
        </div>
        {error && <div className="error-banner conversation-error">{error}</div>}
        {lastDispatch && (
          <div className={'dispatch-result ' + lastDispatch.status}>
            <span>{dispatchStatusLabel(lastDispatch.status)}</span>
            <code>{lastDispatch.applied_strategy ?? lastDispatch.error_code ?? 'accepted'}</code>
            {lastDispatch.current_activation_id && (
              <small>Activation {lastDispatch.current_activation_id}</small>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

function ConversationBubble({
  entry,
  replyable,
  onReply,
}: {
  entry: ConversationEntry
  replyable: boolean
  onReply: (entry: ConversationEntry, option?: string) => void
}) {
  return (
    <article className={'conversation-bubble ' + entry.role + ' ' + entry.kind}>
      <div className="conversation-avatar">
        {entry.role === 'controller' ? <UserRound size={15} /> : <Bot size={15} />}
      </div>
      <div className="conversation-bubble-main">
        <div className="conversation-bubble-head">
          <strong>{entry.role === 'controller' ? '委托者' : 'Agent'}</strong>
          <span>
            {entry.activationNumber ? '激活 ' + entry.activationNumber + ' · ' : ''}
            {formatTime(entry.timestamp)}
          </span>
        </div>
        <MarkdownContent content={entry.text} className="conversation-message-content" />
        {entry.options.length > 0 && !entry.answered && replyable && (
          <div className="question-options">
            {entry.options.map((option) => (
              <button type="button" key={option} onClick={() => onReply(entry, option)}>
                {option}
              </button>
            ))}
          </div>
        )}
        <div className="conversation-bubble-foot">
          <span>{conversationKindLabel(entry.kind)}</span>
          {entry.deliveryStatus && <span>{entry.deliveryStatus}</span>}
          {entry.kind === 'question' && entry.messageId && entry.correlationId && replyable && (
            entry.answered ? (
              <span className="question-answered">已回答</span>
            ) : (
              <button type="button" onClick={() => onReply(entry)}>
                <CornerDownLeft size={12} />回答
              </button>
            )
          )}
        </div>
      </div>
    </article>
  )
}

function buildConversationEntries(
  messages: InteractionMessage[],
  timeline: AgentSessionTurn[],
): ConversationEntry[] {
  const answeredMessageIds = new Set(
    messages.flatMap((message) =>
      message.message_type === 'answer' && message.reply_to ? [message.reply_to] : [],
    ),
  )
  const interactionMessageIds = new Set(messages.map((message) => message.message_id))
  const questionCorrelations = new Set(
    messages.flatMap((message) =>
      message.message_type === 'question' && message.correlation_id
        ? [message.correlation_id]
        : [],
    ),
  )
  const entries: ConversationEntry[] = []

  for (const message of messages) {
    if (!['instruction', 'answer', 'question'].includes(message.message_type)) continue
    const text = messageText(message.payload)
    if (!text) continue
    entries.push({
      key: 'interaction:' + message.message_id,
      role: message.message_type === 'question' ? 'agent' : 'controller',
      kind: message.message_type as 'instruction' | 'answer' | 'question',
      text,
      timestamp: message.created_at,
      sequence: message.sequence,
      deliveryStatus: message.delivery_status,
      messageId: message.message_id,
      correlationId: message.correlation_id ?? undefined,
      answered: answeredMessageIds.has(message.message_id),
      options: stringList(message.payload.options),
    })
  }

  for (const turn of timeline) {
    for (const item of turn.items) {
      if (!['input', 'question', 'message'].includes(item.kind) || !item.text) continue
      if (item.messageId && interactionMessageIds.has(item.messageId)) continue
      if (item.kind === 'question' && questionCorrelations.has(item.correlationId ?? item.itemId)) {
        continue
      }
      entries.push({
        key: 'session:' + turn.key + ':' + item.key,
        role: item.kind === 'input' ? 'controller' : 'agent',
        kind: item.kind === 'input' ? 'instruction' : item.kind === 'question' ? 'question' : 'output',
        text: item.text,
        timestamp: item.updatedAt,
        sequence: item.lastSequence,
        activationNumber: turn.activationNumber ?? undefined,
        messageId: item.messageId,
        correlationId: item.correlationId,
        answered: item.messageId ? answeredMessageIds.has(item.messageId) : false,
        options: item.options,
      })
    }
  }

  return entries.sort((left, right) => {
    const timeDifference = Date.parse(left.timestamp) - Date.parse(right.timestamp)
    return timeDifference === 0 ? left.sequence - right.sequence : timeDifference
  })
}

function messageText(payload: Record<string, unknown>): string {
  for (const field of ['prompt', 'instruction', 'text', 'answer', 'question']) {
    const value = payload[field]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  try {
    return JSON.stringify(payload, null, 2)
  } catch {
    return String(payload)
  }
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && item.length > 0)
    : []
}

function formatTime(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString()
}

function conversationKindLabel(kind: ConversationEntry['kind']): string {
  if (kind === 'instruction') return '指令'
  if (kind === 'answer') return '回答'
  if (kind === 'question') return '提问'
  return 'Agent 输出'
}

function dispatchStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    accepted: '消息已接受',
    queued: '消息已排队',
    dispatching: '正在投递',
    completed: '消息已投递',
    rejected: '消息被拒绝',
    reconciliation_required: '消息需人工对账',
  }
  return labels[status] ?? status
}
