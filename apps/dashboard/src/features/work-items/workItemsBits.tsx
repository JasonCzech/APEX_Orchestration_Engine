/** Tiny shared presentational bits for the work-items screens. */

import { safeExternalHttpUrl } from '@/utils/safeExternalUrl'

import { statusTone } from './workItemsLogic'

/** Kind chip (story/task/bug…) — same primitive the environments KindChip uses. */
export function KindChip({ kind }: { kind: string }) {
  return <span className="dash-context-chip">{kind}</span>
}

/** Status badge with a tone derived from common tracker statuses. */
export function StatusBadge({ status }: { status: string }) {
  return <span className={`status-badge ${statusTone(status)}`}>{status}</span>
}

/** External tracker link (↗) — only rendered when the item carries a URL. */
export function ExternalLink({ url, itemKey }: { url: string; itemKey: string }) {
  const safeUrl = safeExternalHttpUrl(url)
  if (!safeUrl) return null
  return (
    <a
      className="wi-ext-link"
      href={safeUrl}
      target="_blank"
      rel="noreferrer"
      aria-label={`Open ${itemKey} in tracker`}
      title={safeUrl}
    >
      ↗
    </a>
  )
}
