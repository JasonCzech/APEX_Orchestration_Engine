/** Tiny shared presentational bits for the work-items screens. */

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
  return (
    <a
      className="wi-ext-link"
      href={url}
      target="_blank"
      rel="noreferrer"
      aria-label={`Open ${itemKey} in tracker`}
      title={url}
    >
      ↗
    </a>
  )
}
