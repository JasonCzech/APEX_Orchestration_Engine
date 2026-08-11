/** Pure helpers for the work-items screens (environmentsLogic pattern). */

import type { WorkItem } from '@/api/hooks/useWorkTracking'

/** ContextPacket.summary is capped server-side; stay under it. */
const MAX_PACKET_SUMMARY_CHARS = 4_000

/**
 * Detail route for an item. The :provider segment is display context only —
 * the detail fetch keys on the item key alone. When the provider is unknown
 * (e.g. an item created before any query ran), fall back to 'tracker'.
 */
export function workItemPath(
  provider: string | null | undefined,
  key: string,
  project?: string,
  connectionId?: string,
): string {
  const segment = provider?.trim() ? provider.trim() : 'tracker'
  const path = `/work-items/${encodeURIComponent(segment)}/${encodeURIComponent(key)}`
  const params = new URLSearchParams()
  if (project) params.set('project', project)
  if (connectionId) params.set('connection_id', connectionId)
  const query = params.toString()
  return query ? `${path}?${query}` : path
}

/**
 * Console URL that preloads + auto-executes a provider query. Search params
 * (not location state) on purpose: the link survives refresh and can be
 * copied, and the lazy-loaded console reads it on mount.
 */
export function consolePath(
  provider: string,
  query: string,
  project?: string | null,
  connectionId?: string | null,
): string {
  const params = new URLSearchParams({ provider, query })
  if (project) params.set('project', project)
  if (connectionId) params.set('connection_id', connectionId)
  return `/work-items?${params.toString()}`
}

/** Status -> status-badge tone (primitives.css). Unknown statuses stay neutral. */
export function statusTone(status: string): string {
  const normalized = status.trim().toLowerCase().replace(/[\s_-]+/g, '_')
  if (['done', 'closed', 'resolved', 'complete', 'completed', 'accepted'].includes(normalized))
    return 'success'
  if (['in_progress', 'in_review', 'active', 'doing', 'building', 'testing'].includes(normalized))
    return 'info'
  if (['blocked', 'failed', 'rejected'].includes(normalized)) return 'danger'
  if (['pending_approval', 'pending'].includes(normalized)) return 'warning'
  if (['open', 'new', 'todo', 'to_do', 'backlog', 'ready'].includes(normalized)) return 'accent'
  return 'neutral'
}

/**
 * How a provider spells its queries, for labelling the console. Providers the
 * dashboard does not know keep the generic label rather than guessing.
 */
export function queryDialect(
  provider: string,
): { name: string; placeholder: string } | null {
  switch (provider.trim().toLowerCase()) {
    case 'jira':
      return { name: 'JQL', placeholder: 'project = PHX AND statusCategory = "To Do"' }
    case 'ado':
      return {
        name: 'WIQL',
        placeholder: "SELECT [System.Id] FROM WorkItems WHERE [System.State] = 'Active'",
      }
    case 'jira_align':
      return {
        name: 'OData $filter',
        placeholder: "programId eq 42 and itemtype in ('bug')",
      }
    default:
      return null
  }
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

/**
 * Triage context a tracker may or may not expose.
 *
 * Every rich WorkItem field is read here and nowhere else, so a provider that
 * omits one simply contributes no fact rather than rendering an empty row.
 */
export interface WorkItemFact {
  label: string
  value: string
  /** Set for facts that point at another work item. */
  relatedKey?: string
}

export function workItemFacts(item: WorkItem): WorkItemFact[] {
  const facts: WorkItemFact[] = []
  const push = (label: string, value: string | null | undefined) => {
    const text = value?.trim()
    if (text) facts.push({ label, value: text })
  }
  push('Priority', item.priority)
  push('Severity', item.severity)
  push('Sprint', item.sprint)
  push('Assignee', item.assignee)
  if (item.parent_key?.trim()) {
    facts.push({ label: 'Parent', value: item.parent_key.trim(), relatedKey: item.parent_key.trim() })
  }
  for (const linked of item.linked_keys ?? []) {
    if (linked.trim()) facts.push({ label: 'Linked', value: linked.trim(), relatedKey: linked.trim() })
  }
  const labels = (item.labels ?? []).filter((label) => label.trim())
  if (labels.length > 0) facts.push({ label: 'Labels', value: labels.join(', ') })
  return facts
}

/**
 * One-line context-packet summary for a linked work item.
 *
 * This is what story analysis actually reads about a story or defect, so the
 * triage fields belong here and not only on screen.
 */
export function workItemPacketSummary(item: WorkItem): string {
  const parts = [`${item.kind} · ${item.status}`]
  for (const fact of workItemFacts(item)) {
    if (fact.label === 'Linked') continue // relations add little to a one-liner
    parts.push(`${fact.label.toLowerCase()} ${fact.value}`)
  }
  const summary = parts.join(' · ')
  if (summary.length <= MAX_PACKET_SUMMARY_CHARS) return summary
  let clipped = summary.slice(0, MAX_PACKET_SUMMARY_CHARS - 1)
  // Never end on a lone high surrogate: fetch would replace it with U+FFFD.
  const last = clipped.charCodeAt(clipped.length - 1)
  if (last >= 0xd800 && last <= 0xdbff) clipped = clipped.slice(0, -1)
  return clipped + '…'
}

/** Plain-text description -> paragraph blocks (blank-line separated). */
export function descriptionParagraphs(description: string): string[] {
  return description
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter((block) => block.length > 0)
}
