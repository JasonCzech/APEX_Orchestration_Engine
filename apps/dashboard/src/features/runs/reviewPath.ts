export function runReviewPath(
  threadId: string,
  interruptId: string | null | undefined,
  options: {
    source?: 'history' | 'queue' | 'home' | 'overview'
    section?: 'decision' | 'overview' | 'workspace' | 'timeline'
    phase?: string | null
  } = {},
): string {
  const path = `/runs/${encodeURIComponent(threadId)}/reviews/${encodeURIComponent(interruptId ?? 'pending')}`
  const search = new URLSearchParams()
  if (options.source) search.set('source', options.source)
  if (options.section && options.section !== 'decision') search.set('section', options.section)
  if (options.phase) search.set('phase', options.phase)
  return search.size > 0 ? `${path}?${search}` : path
}

export function reviewInterruptId(value: string | null | undefined): string | null {
  return !value || value === 'pending' ? null : value
}
