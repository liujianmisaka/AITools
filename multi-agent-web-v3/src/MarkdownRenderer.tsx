import { useMemo, type ComponentProps } from 'react'
import rehypeKatex from 'rehype-katex'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import 'katex/dist/katex.min.css'
import type { MarkdownContentProps } from './MarkdownContent'

const remarkPlugins: ComponentProps<typeof ReactMarkdown>['remarkPlugins'] = [
  remarkGfm,
  remarkMath,
]
const rehypePlugins: ComponentProps<typeof ReactMarkdown>['rehypePlugins'] = [
  [
    rehypeKatex,
    {
      strict: 'warn',
      trust: false,
      throwOnError: false,
      maxExpand: 1_000,
    },
  ],
]

export default function MarkdownRenderer({
  content,
  className = '',
}: MarkdownContentProps) {
  const normalizedContent = useMemo(() => normalizeMathDelimiters(content), [content])
  return (
    <div className={`markdown-content ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={rehypePlugins}
        skipHtml
        components={{
          a: ({ node: _node, children, ...properties }) => (
            <a {...properties} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          img: ({ node: _node, alt = '', ...properties }) => (
            <img
              {...properties}
              alt={alt}
              loading="lazy"
              referrerPolicy="no-referrer"
            />
          ),
        }}
      >
        {normalizedContent}
      </ReactMarkdown>
    </div>
  )
}

function normalizeMathDelimiters(content: string): string {
  let fence: { marker: string; length: number } | null = null
  return content
    .split('\n')
    .map((line) => {
      const fenceMatch = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line)
      if (fenceMatch !== null) {
        const marker = fenceMatch[1][0]
        const length = fenceMatch[1].length
        if (fence === null) fence = { marker, length }
        else if (
          fence.marker === marker &&
          length >= fence.length &&
          fenceMatch[2].trim().length === 0
        ) {
          fence = null
        }
        return line
      }
      return fence === null ? normalizeMathDelimitersInLine(line) : line
    })
    .join('\n')
}

function normalizeMathDelimitersInLine(line: string): string {
  let normalized = ''
  let codeDelimiterLength = 0
  for (let index = 0; index < line.length; index += 1) {
    if (line[index] === '`') {
      let runLength = 1
      while (line[index + runLength] === '`') runLength += 1
      if (codeDelimiterLength === 0) codeDelimiterLength = runLength
      else if (codeDelimiterLength === runLength) codeDelimiterLength = 0
      normalized += line.slice(index, index + runLength)
      index += runLength - 1
      continue
    }
    const delimiter = line[index + 1]
    if (
      codeDelimiterLength === 0 &&
      line[index] === '\\' &&
      line[index - 1] !== '\\' &&
      (delimiter === '(' || delimiter === ')' || delimiter === '[' || delimiter === ']')
    ) {
      normalized += delimiter === '(' || delimiter === ')' ? '$' : '$$'
      index += 1
      continue
    }
    normalized += line[index]
  }
  return normalized
}
