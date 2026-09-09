export function safeExecutionUrl(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 4096 || /[\s\\]/.test(value)) return null
  try {
    const url = new URL(value)
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search)
      return null
    const match = /^#\/(test|report)\/([a-zA-Z0-9_.:%-]+)(?:\?section=(summary|performance|diagnostics|evidence))?$/.exec(url.hash)
    if (!match || (match[1] === 'test' && match[3])) return null
    const runId = decodeURIComponent(match[2]!)
    if (!/^[a-zA-Z0-9_.:-]+$/.test(runId)) return null
    return url.href
  } catch {
    return null
  }
}

/** Explicit navigation only; no authentication or executable payload crosses apps. */
export function executionLink(value: unknown, returnPath: string, section?: 'diagnostics' | 'evidence'): string | null {
  const safe = safeExecutionUrl(value)
  if (!safe) return null
  const url = new URL(safe)
  url.searchParams.set('orchestrationReturn', new URL(returnPath, window.location.origin).href)
  const executionId = url.hash.split('/')[2]!.split('?')[0]!
  url.searchParams.set('orchestrationRun', decodeURIComponent(executionId))
  if (section) url.hash = `/report/${executionId}?section=${section}`
  return url.href
}
