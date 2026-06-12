/** Tiny relative-time formatter for grid timestamps (no dependency). */

const EM_DASH = '—'

/**
 * Formats an ISO timestamp relative to `now` (injectable for tests):
 * "just now" / "5m ago" / "3h ago" / "2d ago", falling back to a short
 * locale date beyond a week. Null/invalid input renders an em dash.
 */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return EM_DASH
  const timestamp = Date.parse(iso)
  if (Number.isNaN(timestamp)) return EM_DASH

  const seconds = Math.round((now - timestamp) / 1000)
  if (seconds < 45) return 'just now'
  if (seconds < 90) return '1m ago'

  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`

  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`

  const days = Math.round(hours / 24)
  if (days < 7) return `${days}d ago`

  const date = new Date(timestamp)
  const sameYear = date.getFullYear() === new Date(now).getFullYear()
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  })
}
