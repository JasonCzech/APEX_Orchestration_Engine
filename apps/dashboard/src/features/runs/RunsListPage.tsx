import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import { usePipelines, type PipelineSummary } from '@/api/hooks/usePipelines'
import { isApiError } from '@/api/errors'
import { PhaseStrip } from '@/components/runs/PhaseStrip'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import { LaunchRunButton } from './LaunchRunButton'
import {
  hasActiveFilters,
  parseRunsFilters,
  serializeRunsFilters,
  THREAD_STATUSES,
  isThreadStatus,
  type RunsFilters,
} from './runsFilters'
import './RunsListPage.css'

const SEARCH_DEBOUNCE_MS = 300
const SKELETON_ROWS = 6
const EM_DASH = '—'

/** status-badge tone per LangGraph thread_status. */
function statusTone(status: string | null | undefined): string {
  switch (status) {
    case 'busy':
      return 'accent'
    case 'interrupted':
      return 'warning'
    case 'error':
      return 'danger'
    case 'idle':
      return 'success'
    default:
      return 'neutral'
  }
}

function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return 'The runs list could not be loaded.'
}

function RunRow({ run }: { run: PipelineSummary }) {
  const navigate = useNavigate()
  const runPath = `/runs/${run.thread_id}`

  return (
    <tr
      className="runs-row"
      onClick={() => navigate(runPath)}
      data-testid={`runs-row-${run.thread_id}`}
    >
      <td>
        <Link to={runPath} className="runs-run-link" onClick={(event) => event.stopPropagation()}>
          <span className="runs-run-title strong">{run.title || 'Untitled run'}</span>
          <span className="runs-run-id">{run.thread_id}</span>
        </Link>
      </td>
      <td>
        <div className="runs-status-cell">
          <span className={`status-badge ${statusTone(run.thread_status)}`}>
            {run.thread_status ?? 'unknown'}
          </span>
          {run.pending_gate && (
            <Link
              to={runPath}
              className="topbar-meta-chip warning runs-gate-chip"
              title={
                run.pending_gate.phase ? `Pending gate on ${run.pending_gate.phase}` : undefined
              }
              onClick={(event) => event.stopPropagation()}
            >
              gate: {run.pending_gate.kind ?? 'review'}
            </Link>
          )}
        </div>
      </td>
      <td>
        <PhaseStrip
          strip={run.phase_strip}
          size="md"
          onSelect={(phase) => navigate(`${runPath}/phases/${phase}`)}
        />
      </td>
      <td>
        {run.engine?.engine ? (
          <span className="dash-context-chip" title={run.engine.external_run_id ?? undefined}>
            {run.engine.engine}
          </span>
        ) : (
          <span className="runs-muted">{EM_DASH}</span>
        )}
      </td>
      <td className="runs-time" title={run.created_at ?? undefined}>
        {formatRelative(run.created_at)}
      </td>
      <td className="runs-time" title={run.updated_at ?? undefined}>
        {formatRelative(run.updated_at)}
      </td>
    </tr>
  )
}

function RunsSkeleton() {
  return (
    <div className="runs-skeleton" role="status" aria-busy="true" aria-label="Loading runs">
      {Array.from({ length: SKELETON_ROWS }, (_, i) => (
        <div key={i} className="glass-panel runs-skeleton-row" />
      ))}
    </div>
  )
}

/**
 * /runs — history grid (plan UX 2.d). Filters live in the URL (deep-linkable,
 * back/forward safe); the list polls every 15s while the tab is visible and
 * keeps the previous page rendered during transitions.
 */
export function RunsListPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => parseRunsFilters(searchParams), [searchParams])

  const applyFilters = useCallback(
    (patch: Partial<RunsFilters>) => {
      setSearchParams((prev) => serializeRunsFilters({ ...parseRunsFilters(prev), ...patch }))
    },
    [setSearchParams],
  )

  // Search box: local echo state, committed to the URL after a 300ms debounce.
  const [search, setSearch] = useState(filters.q ?? '')
  const committedQ = filters.q ?? ''
  useEffect(() => {
    // Back/forward (or clear-filters) changed the URL: resync the input.
    setSearch(committedQ)
  }, [committedQ])
  useEffect(() => {
    const trimmed = search.trim()
    if (trimmed === committedQ) return undefined
    const id = window.setTimeout(() => {
      applyFilters({ q: trimmed || undefined, offset: 0 })
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(id)
  }, [search, committedQ, applyFilters])

  const { data, error, isPending, isError, refetch } = usePipelines(filters)

  const items = data?.items ?? []
  const total = data?.total
  const prevDisabled = filters.offset === 0
  const nextDisabled =
    total !== undefined
      ? filters.offset + filters.limit >= total
      : items.length < filters.limit
  const rangeCaption =
    items.length > 0
      ? `${filters.offset + 1}–${filters.offset + items.length}${total !== undefined ? ` of ${total}` : ''}`
      : 'No runs'

  const clearFilters = () => {
    setSearch('')
    setSearchParams(new URLSearchParams())
  }

  return (
    <section className="runs-page animate-enter">
      <header className="runs-toolbar glass-panel">
        <input
          type="search"
          className="field-input runs-search"
          placeholder="Search runs…"
          aria-label="Search runs"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="field-select"
          aria-label="Filter by status"
          value={filters.status ?? ''}
          onChange={(event) => {
            const value = event.target.value
            applyFilters({ status: isThreadStatus(value) ? value : undefined, offset: 0 })
          }}
        >
          <option value="">All statuses</option>
          {THREAD_STATUSES.map((status) => (
            <option key={status} value={status}>
              {status}
            </option>
          ))}
        </select>
        {hasActiveFilters(filters) && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={clearFilters}>
            Clear filters
          </button>
        )}
        {/* D2 minimal launch (live-UI agent); full wizard lands on /runs/new in D4. */}
        <LaunchRunButton />
      </header>

      {isPending ? (
        <RunsSkeleton />
      ) : isError && !data ? (
        <ProblemCard title="Runs unavailable" message={errorMessage(error)} onRetry={() => refetch()} />
      ) : items.length === 0 ? (
        <div className="dash-empty">
          <h2>No runs found</h2>
          {hasActiveFilters(filters) ? (
            <>
              <p className="dash-empty-hint">No runs match the current filters.</p>
              <button type="button" className="btn btn-secondary" onClick={clearFilters}>
                Clear filters
              </button>
            </>
          ) : (
            <>
              <p className="dash-empty-hint">Launch your first pipeline run to see it here.</p>
              <Link to="/runs/new" className="btn btn-primary runs-empty-cta">
                Start a new run
              </Link>
            </>
          )}
        </div>
      ) : (
        <>
          {isError && (
            <div className="runs-refresh-error" role="alert">
              <span>Refresh failed: {errorMessage(error)}</span>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => refetch()}>
                Retry
              </button>
            </div>
          )}
          <div className="data-table-wrap">
            <table className="data-table striped runs-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Status</th>
                  <th>Phases</th>
                  <th>Engine</th>
                  <th>Created</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {items.map((run) => (
                  <RunRow key={run.thread_id} run={run} />
                ))}
              </tbody>
            </table>
          </div>
          <footer className="runs-pagination">
            <span className="runs-pagination-caption">{rangeCaption}</span>
            <div className="runs-pagination-buttons">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={prevDisabled}
                onClick={() =>
                  applyFilters({ offset: Math.max(0, filters.offset - filters.limit) })
                }
              >
                Previous
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={nextDisabled}
                onClick={() => applyFilters({ offset: filters.offset + filters.limit })}
              >
                Next
              </button>
            </div>
          </footer>
        </>
      )}
    </section>
  )
}
