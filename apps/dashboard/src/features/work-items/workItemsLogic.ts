/** Pure helpers for the work-items screens (environmentsLogic pattern). */

/**
 * Detail route for an item. The :provider segment is display context only —
 * the detail fetch keys on the item key alone. When the provider is unknown
 * (e.g. an item created before any query ran), fall back to 'tracker'.
 */
export function workItemPath(
  provider: string | null | undefined,
  key: string,
  project?: string,
): string {
  const segment = provider?.trim() ? provider.trim() : 'tracker'
  const path = `/work-items/${encodeURIComponent(segment)}/${encodeURIComponent(key)}`
  return project ? `${path}?${new URLSearchParams({ project }).toString()}` : path
}

/**
 * Console URL that preloads + auto-executes a provider query. Search params
 * (not location state) on purpose: the link survives refresh and can be
 * copied, and the lazy-loaded console reads it on mount.
 */
export function consolePath(provider: string, query: string, project?: string | null): string {
  const params = new URLSearchParams({ provider, query })
  if (project) params.set('project', project)
  return `/work-items?${params.toString()}`
}

/** Status -> status-badge tone (primitives.css). Unknown statuses stay neutral. */
export function statusTone(status: string): string {
  const normalized = status.trim().toLowerCase().replace(/[\s_-]+/g, '_')
  if (['done', 'closed', 'resolved', 'complete', 'completed'].includes(normalized)) return 'success'
  if (['in_progress', 'in_review', 'active', 'doing'].includes(normalized)) return 'info'
  if (['blocked', 'failed', 'rejected'].includes(normalized)) return 'danger'
  if (['open', 'new', 'todo', 'to_do', 'backlog'].includes(normalized)) return 'accent'
  return 'neutral'
}

/** '' and whitespace count as {}; otherwise must parse to a plain JSON object. */
export function parseJsonObject(
  raw: string,
): { ok: true; value: Record<string, unknown> } | { ok: false; message: string } {
  const trimmed = raw.trim()
  if (!trimmed) return { ok: true, value: {} }
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { ok: false, message: 'Fields must be a JSON object.' }
    }
    return { ok: true, value: parsed as Record<string, unknown> }
  } catch {
    return { ok: false, message: 'Fields are not valid JSON.' }
  }
}

/** Plain-text description -> paragraph blocks (blank-line separated). */
export function descriptionParagraphs(description: string): string[] {
  return description
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter((block) => block.length > 0)
}
