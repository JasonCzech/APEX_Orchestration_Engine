import { useMemo } from 'react'

import { CodeViewer } from './CodeViewer'

function prettify(value: unknown): string {
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2)
    } catch {
      return value // not valid JSON — show the raw text rather than nothing
    }
  }
  return JSON.stringify(value, null, 2) ?? ''
}

/**
 * Collapsible JSON viewer: pretty-prints (strings are parsed first) and renders
 * through the read-only CodeMirror surface with JSON highlighting + fold gutter.
 */
export function JsonViewer({ value, ariaLabel }: { value: unknown; ariaLabel?: string }) {
  const text = useMemo(() => prettify(value), [value])
  return <CodeViewer value={text} language="json" ariaLabel={ariaLabel ?? 'JSON viewer'} />
}
