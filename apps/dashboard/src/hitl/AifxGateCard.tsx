/**
 * Prompt-review sidebar card for AIFx Context Services: which repositories the
 * run pinned (repo @ commit) and which entries this phase's agent will follow.
 * The gate payload carries metadata only; "Show pinned content" reads the run's
 * immutable snapshot from GET /v1/pipelines/{thread_id}/aifx. Read-only:
 * AIFx content is changed in its repository, never at a gate.
 */
import { useState } from 'react'

import type { AifxGatePreview, PhaseName } from '@apex/pipeline-events'

import { usePipelineAifx } from '@/api/hooks/useAifx'

const ROLE_LABEL: Record<string, string> = {
  'global-rules': 'Global rules',
  'team-documents': 'Team documents',
  'application-context': 'ACB',
}

export function AifxGateCard({
  aifx,
  threadId,
  phase,
}: {
  aifx: AifxGatePreview
  threadId?: string
  phase?: PhaseName
}) {
  const [open, setOpen] = useState(false)
  const pinned = usePipelineAifx(threadId, open)
  const entryCount = Math.max(aifx.entry_count ?? aifx.entries.length, aifx.entries.length)
  const issueCount = Math.max(aifx.issue_count ?? aifx.issues.length, aifx.issues.length)
  const truncated = entryCount > aifx.entries.length
  const wanted = new Set(aifx.entries.map((entry) => `${entry.repo}:${entry.id}`))
  // The gate lists at most 64 titles; with the phase known, show every entry it follows.
  const entries = (pinned.data?.snapshot?.entries ?? []).filter((entry) =>
    truncated && phase
      ? entry.phases === 'all' || (Array.isArray(entry.phases) && entry.phases.includes(phase))
      : wanted.has(`${entry.repo}:${entry.id}`),
  )

  return (
    <section className="gate-info-card" data-testid="gate-aifx">
      <h4 className="gate-field-label">AIFx context</h4>
      {aifx.status === 'degraded' && (
        <div className="gate-info-copy" role="status">
          <p>
            Launched without some sources ({issueCount} problem{issueCount === 1 ? '' : 's'}):
          </p>
          <ul className="gate-aifx-issues">
            {aifx.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
          {issueCount > aifx.issues.length && <p>…and {issueCount - aifx.issues.length} more.</p>}
        </div>
      )}
      <ul className="gate-packet-list">
        {aifx.sources.map((source) => (
          <li key={`${source.role}:${source.repo}`} className="gate-packet">
            <span className="gate-packet-title">
              {source.application_name ? `${source.application_name} · ` : ''}
              {source.repo}
            </span>
            <span className="kind-chip">{ROLE_LABEL[source.role] ?? source.role}</span>
            <span className="gate-packet-summary">
              {source.ref} @{' '}
              {source.web_url ? (
                <a href={source.web_url} target="_blank" rel="noreferrer">
                  <code>{source.commit_sha.slice(0, 7)}</code>
                </a>
              ) : (
                <code>{source.commit_sha.slice(0, 7)}</code>
              )}
            </span>
          </li>
        ))}
      </ul>
      {aifx.entries.length > 0 ? (
        <p className="gate-info-copy">
          This phase follows {entryCount} entr{entryCount === 1 ? 'y' : 'ies'}
          {truncated ? ` (first ${aifx.entries.length} shown)` : ''}:{' '}
          {aifx.entries.map((entry) => entry.title).join(', ')}
          {truncated ? ', …' : '.'}
        </p>
      ) : (
        <p className="gate-info-copy">No AIFx entries apply to this phase.</p>
      )}
      {threadId && aifx.entries.length > 0 && (
        <details onToggle={(event) => setOpen((event.target as HTMLDetailsElement).open)}>
          <summary>Show pinned content (read-only)</summary>
          {pinned.isPending && open && <p className="gate-info-copy">Loading…</p>}
          {pinned.isError && <p className="gate-info-copy" role="alert">{pinned.error.message}</p>}
          {entries.map((entry) => (
            <div key={`${entry.repo}:${entry.id}`} data-testid="gate-aifx-entry">
              <p className="gate-info-copy">
                <strong>{entry.title}</strong> <span className="kind-chip">{entry.type}</span>{' '}
                <code>{entry.path}</code>
              </p>
              <pre className="gate-aifx-content">{entry.content}</pre>
            </div>
          ))}
        </details>
      )}
    </section>
  )
}
