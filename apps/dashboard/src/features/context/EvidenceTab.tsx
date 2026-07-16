/**
 * Evidence tab — project + optional thread filters (committed on submit) over
 * GET /v1/context/evidence; packets render as cards grouped by source with a
 * /runs/{thread_id} deep link when the packet carries one.
 */
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router'

import { useEvidence } from '@/api/hooks/useContextApi'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'

import { groupEvidence } from './contextLogic'

const SKELETON_ROWS = 3

export function EvidenceTab() {
  const [projectDraft, setProjectDraft] = useState('')
  const [threadDraft, setThreadDraft] = useState('')
  const [filters, setFilters] = useState<{ project?: string; thread?: string }>({})

  const evidence = useEvidence(filters.project, filters.thread)

  function applyFilters(event: FormEvent) {
    event.preventDefault()
    setFilters({
      project: projectDraft.trim() || undefined,
      thread: threadDraft.trim() || undefined,
    })
  }

  const groups = groupEvidence(evidence.data ?? [])

  return (
    <>
      <form className="ctx-toolbar glass-panel" aria-label="Evidence filters" onSubmit={applyFilters}>
        <input
          type="text"
          className="field-input"
          aria-label="Filter by project"
          placeholder="Project (proj-alpha)"
          value={projectDraft}
          onChange={(event) => setProjectDraft(event.target.value)}
        />
        <input
          type="text"
          className="field-input ctx-grow"
          aria-label="Filter by thread"
          placeholder="Thread id (optional)"
          value={threadDraft}
          onChange={(event) => setThreadDraft(event.target.value)}
        />
        <button type="submit" className="btn btn-secondary btn-sm">
          Apply
        </button>
      </form>

      {evidence.isError && evidence.data && (
        <CachedDataWarning error={evidence.error} onRetry={() => void evidence.refetch()} />
      )}

      {evidence.isPending ? (
        <div className="ctx-skeleton" role="status" aria-busy="true" aria-label="Loading evidence">
          {Array.from({ length: SKELETON_ROWS }, (_, i) => (
            <div key={i} className="glass-panel ctx-skeleton-row" />
          ))}
        </div>
      ) : evidence.isError && !evidence.data ? (
        <ProblemCard
          title="Evidence unavailable"
          message={evidence.error.message}
          onRetry={() => void evidence.refetch()}
        />
      ) : groups.length === 0 ? (
        <div className="dash-empty">
          <h2>No evidence yet</h2>
          <p className="dash-empty-hint">
            Evidence packets accrue automatically as pipeline runs execute — phase outputs,
            tracker lookups and log findings land here, scoped to their project and thread.
          </p>
        </div>
      ) : (
        groups.map((group) => (
          <section
            key={group.source}
            className="ctx-evidence-group"
            aria-label={`Source ${group.source}`}
          >
            <header className="ctx-evidence-group-header">
              <h3 className="ctx-evidence-group-title">{group.source}</h3>
              <span className="ctx-caption">
                {group.packets.length} packet{group.packets.length === 1 ? '' : 's'}
              </span>
            </header>
            <div className="ctx-packet-grid">
              {group.packets.map((packet, index) => (
                <article
                  key={packet.id ?? `${group.source}-${index}`}
                  className="ctx-packet glass-panel"
                  data-testid={packet.id ? `evidence-${packet.id}` : undefined}
                >
                  <span className="dash-context-chip">{packet.source}</span>
                  <h4 className="ctx-packet-title">{packet.title}</h4>
                  {packet.summary && <p className="ctx-packet-summary">{packet.summary}</p>}
                  <footer className="ctx-packet-footer">
                    {packet.ref ? (
                      <span className="ctx-packet-ref" title={packet.ref}>
                        {packet.ref}
                      </span>
                    ) : (
                      <span aria-hidden="true" />
                    )}
                    {packet.thread_id && (
                      <Link className="btn btn-ghost btn-sm" to={`/runs/${packet.thread_id}`}>
                        Open run
                      </Link>
                    )}
                  </footer>
                </article>
              ))}
            </div>
          </section>
        ))
      )}
    </>
  )
}
