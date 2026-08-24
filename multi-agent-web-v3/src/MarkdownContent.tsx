import { lazy, memo, Suspense } from 'react'

export type MarkdownContentProps = {
  content: string
  className?: string
}

type FormattedOutputProps = {
  output: unknown
  className?: string
}

const MarkdownRenderer = lazy(() => import('./MarkdownRenderer'))

export const MarkdownContent = memo(function MarkdownContent({
  content,
  className = '',
}: MarkdownContentProps) {
  return (
    <Suspense
      fallback={<pre className={`markdown-loading ${className}`.trim()}>{content}</pre>}
    >
      <MarkdownRenderer content={content} className={className} />
    </Suspense>
  )
})

export function FormattedOutput({ output, className = '' }: FormattedOutputProps) {
  const markdown = markdownText(output)
  const structured = typeof output !== 'string'
  return (
    <div className={`formatted-output ${className}`.trim()}>
      {markdown === null ? (
        <pre className="structured-output">{formatStructured(output)}</pre>
      ) : (
        <>
          <MarkdownContent content={markdown} />
          {structured && (
            <details className="structured-output-details">
              <summary>查看结构化数据</summary>
              <pre>{formatStructured(output)}</pre>
            </details>
          )}
        </>
      )}
    </div>
  )
}

function markdownText(value: unknown, depth = 0): string | null {
  if (typeof value === 'string') return value
  if (depth > 2 || typeof value !== 'object' || value === null || Array.isArray(value)) {
    return null
  }
  const record = value as Record<string, unknown>
  if (typeof record.answer === 'string') return record.answer
  return markdownText(record.output, depth + 1)
}

function formatStructured(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    return String(value)
  }
}
