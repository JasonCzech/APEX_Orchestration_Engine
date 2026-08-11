/** Tiny shared presentational bits for the work-items screens. */

import { Link } from 'react-router'

import { safeExternalHttpUrl } from '@/utils/safeExternalUrl'

import { statusTone, workItemPath, type WorkItemFact } from './workItemsLogic'

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

/** Tracker labels, rendered inline under a title. Absent labels render nothing. */
export function LabelChips({ labels }: { labels?: string[] | null }) {
  const visible = (labels ?? []).filter((label) => label.trim())
  if (visible.length === 0) return null
  return (
    <span className="wi-label-chips">
      {visible.map((label) => (
        <span key={label} className="dash-context-chip adm-chip-muted">
          {label}
        </span>
      ))}
    </span>
  )
}

/**
 * Triage facts as a definition list. Facts that name another work item link to
 * it, so an epic or a blocking defect is one click away.
 */
export function FactsGrid({
  facts,
  provider,
  project,
  connectionId,
}: {
  facts: WorkItemFact[]
  provider?: string | null
  project?: string
  connectionId?: string
}) {
  if (facts.length === 0) return null
  return (
    <dl className="wi-facts" data-testid="wi-facts">
      {facts.map((fact, index) => (
        <div className="wi-fact" key={`${fact.label}:${fact.value}:${index}`}>
          <dt className="wi-fact-label">{fact.label}</dt>
          <dd className="wi-fact-value">
            {fact.relatedKey ? (
              <Link
                className="wi-key-link"
                to={workItemPath(provider, fact.relatedKey, project, connectionId)}
              >
                {fact.value}
              </Link>
            ) : (
              fact.value
            )}
          </dd>
        </div>
      ))}
    </dl>
  )
}
