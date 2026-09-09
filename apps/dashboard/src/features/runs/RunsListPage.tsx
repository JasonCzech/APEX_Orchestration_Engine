import { orchestrationTone, formatTimestamp } from '@/features/runs/runDisplay'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'


import { usePipelines, type PipelineSummary } from '@/api/hooks/usePipelines'
import { PHASE_NAMES } from '@apex/pipeline-events'
import { useCompactLayout } from '@/components/hooks/useCompactLayout'
import { useThreadState } from '@/api/hooks/useThreadState'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
import { isApiError } from '@/api/errors'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { JsonViewer } from '@/components/viewers/JsonViewer'

import { CompareSelectBar } from '../compare/CompareSelectBar'
import { MAX_COMPARE_RUNS } from '../compare/compareModel'
import { OverflowMenu, PreflightModal } from './PreflightModal'
import {
  hasActiveFilters,
  parseRunsFilters,
  RUNS_MAX_OFFSET,
  serializeRunsFilters,
  THREAD_STATUSES,
  isThreadStatus,
  type RunsFilters,
} from './runsFilters'
import {
  isPipelinePhaseComplete,
  pipelineStatusLabel,
  pipelinePhaseVisual,
  executionResultLabel,
  orchestrationStatusLabel,
  gateLabel,
  phaseProgress,
  normalizedPhases,
  targetPhaseFor,
} from './runDisplay'
import { usePreferences } from '@/theme/preferences'
import { formatDuration, PHASE_LABELS, isPhaseName } from './runDisplay'
import './RunsListPage.css'

const SEARCH_DEBOUNCE_MS = 300
const SKELETON_ROWS = 6
const EM_DASH = '—'

/** status-badge tone per LangGraph thread_status. */

function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return 'The runs list could not be loaded.'
}

function verdictTone(run: PipelineSummary): string {
  switch (executionResultLabel(run)) {
    case 'Passed':
      return 'success'
        case 'Failed':
      return 'danger'
    default:
      return 'neutral'
  }
}

function completedPhases(run: PipelineSummary): number {
  return run.phase_strip.filter((entry) => isPipelinePhaseComplete(entry.status)).length
}

function durationLabel(run: PipelineSummary): string {
  if (!run.created_at || !run.updated_at) return EM_DASH
  const created = Date.parse(run.created_at)
  const updated = Date.parse(run.updated_at)
  if (Number.isNaN(created) || Number.isNaN(updated) || updated < created) return EM_DASH
  return formatDuration((updated - created) / 1000)
}

/** D8 compare affordance: checkbox column state for one row (undefined = off). */
interface RowSelection {
  selected: boolean
  disabled: boolean
  onToggle: (threadId: string) => void
}

function RunRow({
  run,
  onRerun,
  onInspect,
  inspected,
  selection,
  canRerun,
}: {
  run: PipelineSummary
  onRerun: (threadId: string) => void
  onInspect: (threadId: string) => void
  inspected: boolean
  selection?: RowSelection
  canRerun: boolean
}) {
  const navigate = useNavigate()
  const runPath = `/runs/${run.thread_id}`
  const displayPhase = run.pending_gate?.phase ?? run.current_phase
  const compact = useCompactLayout()
  if (compact) return (
    <article className="glass-panel run-card" data-testid={`runs-row-${run.thread_id}`}>
      <Link to={runPath} className="runs-run-title">{run.title || 'Untitled run'}</Link>
      <code className="runs-run-identity">{run.thread_id}</code>
      <p><strong>{orchestrationStatusLabel(run.thread_status, run.phase_strip)}</strong></p>
      <p>Test result: <strong>{executionResultLabel(run)}</strong></p>
      <p>Progress: {completedPhases(run)}/{phaseProgress(run.phase_strip).total} phases finished</p>
      {run.pending_gate && <Link to={runPath}>{gateLabel(run.pending_gate.kind)}</Link>}
      {selection && <label><input type="checkbox" checked={selection.selected} disabled={selection.disabled} onChange={() => selection.onToggle(run.thread_id)} /> Compare {run.title}</label>}
      <details><summary>Run information</summary><dl><dt>Project</dt><dd>{run.project_id ?? EM_DASH}</dd><dt>Application</dt><dd>{run.app_id ?? EM_DASH}</dd><dt>Created</dt><dd>{formatTimestamp(run.created_at)}</dd><dt>Duration</dt><dd>{durationLabel(run)}</dd></dl></details>
      <div className="runs-actions-stack"><button className="btn btn-secondary" aria-label={`Inspect ${run.title || 'Untitled run'} (${run.thread_id})`} onClick={() => onInspect(run.thread_id)}>Inspect</button>{canRerun && <button className="btn btn-ghost" onClick={() => onRerun(run.thread_id)}>Re-run…</button>}</div>
    </article>
  )

  return (
    <tr className="runs-row" data-testid={`runs-row-${run.thread_id}`}>
      {selection && (
        <td className="runs-select-cell">
          <input
            type="checkbox"
            checked={selection.selected}
            disabled={selection.disabled}
            aria-label={`Select ${run.title || 'Untitled run'} for compare`}
            title={
              selection.disabled ? `Comparison is limited to ${MAX_COMPARE_RUNS} runs` : undefined
            }
            onChange={() => selection.onToggle(run.thread_id)}
          />
        </td>
      )}
      <td>
        <Link to={runPath} className="runs-run-link">
          <span className="runs-run-title strong">{run.title || 'Untitled run'}</span>
          <code className="runs-run-identity">{run.thread_id}</code>
          <span className="runs-run-id">{isPhaseName(displayPhase) ? PHASE_LABELS[displayPhase] : 'Phase not recorded'}</span>
        </Link>
      </td>
      <td>
        <span className="runs-story-cell">{run.project_id ?? EM_DASH}</span>
      </td>
      <td>
        <span className="runs-application-cell">{run.app_id ?? EM_DASH}</span>
      </td>
      <td>
        <span className="runs-phase-progress">
          {completedPhases(run)}/{phaseProgress(run.phase_strip).total}
        </span>
      </td>
      <td className="runs-time" title={run.created_at ?? undefined}>
        {formatTimestamp(run.created_at)}
      </td>
      <td className="runs-time">{durationLabel(run)}</td>
      <td>
        <span className={`status-badge ${verdictTone(run)}`}>{executionResultLabel(run)}</span>
      </td>
      <td>
        <div className="runs-status-cell">
          <span className={`status-badge ${orchestrationTone(run.thread_status, run.phase_strip)}`}>
            {orchestrationStatusLabel(run.thread_status, run.phase_strip)}
          </span>
          {run.pending_gate && (
            <Link
              to={runPath}
              className="topbar-meta-chip warning runs-gate-chip"
              title={
                run.pending_gate.phase ? `Pending gate on ${run.pending_gate.phase}` : undefined
              }
            >
              {gateLabel(run.pending_gate.kind)}
            </Link>
          )}
        </div>
      </td>
      <td className="runs-actions-cell">
        <div className="runs-actions-stack">
          <button
            type="button"
            className={`btn btn-sm ${inspected ? 'btn-secondary' : 'btn-ghost'}`}
            aria-label={`Inspect ${run.title || 'Untitled run'} (${run.thread_id})`} onClick={() => onInspect(run.thread_id)}
          >
            Inspect
          </button>
          <OverflowMenu
            label={`Run actions: ${run.title || run.thread_id}`}
            items={[
              ...(canRerun ? [{ label: 'Re-run…', onSelect: () => onRerun(run.thread_id) }] : []),
              { label: 'Open overview', onSelect: () => void navigate(runPath) },
              { label: 'Copy run ID', onSelect: () => { void navigator.clipboard?.writeText(run.thread_id) } },
            ]}
          />
        </div>
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
  const { preferences } = usePreferences()
  const compact = useCompactLayout()
  const consumer = useOptionalConsumer()
  const canRerun = consumer === undefined || (consumer !== null && roleAtLeast(consumer.role, 'operator'))
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => ({...parseRunsFilters(searchParams), ...(!searchParams.has('limit') ? {limit:preferences.rows} : {})}), [searchParams, preferences.rows])
  // D4: row overflow "Re-run…" opens the pre-flight modal for that thread
  // (the modal fetches thread state itself; preselection = last plan).
  const [rerunThreadId, setRerunThreadId] = useState<string | null>(null)
  // D8: [Compare] toolbar toggle reveals a checkbox column; 2+ ticks float a
  // "Compare (N)" action bar linking to /runs/compare?ids=… (selection is
  // page-local view state, deliberately not in the URL).
  const [compareMode, setCompareMode] = useState(false)
  const [compareSelection, setCompareSelection] = useState<string[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const inspector = useThreadState(selectedRunId ?? undefined)
  const [selectedInspectorPhase, setSelectedInspectorPhase] = useState<string | null>(null)
  const toggleCompareMode = () => {
    setCompareSelection([])
    setCompareMode(!compareMode)
  }
  const toggleCompareSelection = (threadId: string) => {
    setCompareSelection((prev) =>
      prev.includes(threadId) ? prev.filter((id) => id !== threadId) : [...prev, threadId],
    )
  }

  const applyFilters = useCallback(
    (patch: Partial<RunsFilters>) => {
      setSearchParams((prev) => {
        const next = { ...parseRunsFilters(prev), limit: prev.has('limit') ? parseRunsFilters(prev).limit : preferences.rows, ...patch }
        const params = serializeRunsFilters(next)
        params.set('limit', String(next.limit))
        return params
      })
    },
    [setSearchParams, preferences.rows],
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

  const { data, error, isPending, isError, isPlaceholderData, refetch } = usePipelines(filters)

  const items = data?.items ?? []
  const total = data?.total
  const prevDisabled = filters.offset === 0 || isPlaceholderData
  const nextDisabled =
    filters.offset >= RUNS_MAX_OFFSET ||
    (total !== undefined
      ? filters.offset + filters.limit >= total
      : items.length < filters.limit || isPlaceholderData)
  const reachedResultWindow =
    filters.offset >= RUNS_MAX_OFFSET &&
    (total !== undefined
      ? filters.offset + items.length < total
      : items.length === filters.limit)
  const partialSearch = Boolean(filters.q && data?.partial)
  const rangeCaption =
    items.length > 0
      ? `${filters.offset + 1}–${filters.offset + items.length}${total !== undefined ? ` of ${total}` : ''}`
      : filters.offset > 0
        ? 'No more runs'
        : 'No runs'

  const clearFilters = () => {
    setSearch('')
    setSearchParams(new URLSearchParams())
  }

  useEffect(() => {
    if (!inspector.data) return
    const phase = targetPhaseFor(inspector.data.detail, inspector.data.state)
    setSelectedInspectorPhase((current) =>
      current && PHASE_NAMES.includes(current as (typeof PHASE_NAMES)[number]) ? current : phase,
    )
  }, [inspector.data])

  const paginationFooter = (
    <>
      {reachedResultWindow && (
        <p className="runs-result-window-note" role="status">
          Reached the runs result-window limit. Refine the filters to inspect later matches.
        </p>
      )}
      {partialSearch && (
        <p className="runs-result-window-note" role="status">
          Search reached the recent-run scan limit. Refine the filters to search a smaller set.
        </p>
      )}
      <footer className="runs-pagination">
        <span className="runs-pagination-caption">{rangeCaption}</span>
        <div className="runs-pagination-buttons">
          <label>Rows <select className="field-select" aria-label="Rows per table" value={filters.limit} onChange={event => applyFilters({limit:Number(event.target.value),offset:0})}>{Array.from(new Set([10,25,50,100,filters.limit])).sort((a,b)=>a-b).map(size => <option key={size} value={size}>{size}</option>)}</select></label>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={prevDisabled}
            onClick={() => applyFilters({ offset: Math.max(0, filters.offset - filters.limit) })}
          >
            Previous
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={nextDisabled || isPlaceholderData}
            onClick={() =>
              applyFilters({
                offset: Math.min(RUNS_MAX_OFFSET, filters.offset + filters.limit),
              })
            }
          >
            Next
          </button>
        </div>
      </footer>
    </>
  )

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
              {orchestrationStatusLabel(status)}
            </option>
          ))}
        </select>
        {hasActiveFilters(filters) && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={clearFilters}>
            Clear filters
          </button>
        )}
        <button
          type="button"
          className={`btn btn-sm ${compareMode ? 'btn-secondary' : 'btn-ghost'}`}
          aria-pressed={compareMode}
          onClick={toggleCompareMode}
        >
          Compare
        </button>
        {/* D2 minimal launch (live-UI agent); full wizard lands on /runs/new in D4. */}
        <Link to="/runs/new" className="btn btn-primary">New Test</Link>
      </header>
      <details className="ux-filters"><summary>Filters{hasActiveFilters(filters) ? ' · Active' : ''}</summary><div>
        <label className="ux-field">Project<input className="field-input" aria-label="Filter by project" value={filters.project ?? ''} onChange={event => applyFilters({project:event.target.value || undefined,offset:0})} /></label>
        <p className="ux-context">Filters apply to the accessible run history. Use search to narrow run titles.</p>
      </div></details>
      {compareMode && <p role="status" className="ux-context">Select 2–{MAX_COMPARE_RUNS} runs to compare their results and evidence.</p>}

      {isError && (!data || isPlaceholderData) ? (
        <ProblemCard title="Runs unavailable" message={errorMessage(error)} onRetry={() => refetch()} />
      ) : isPending || isPlaceholderData ? (
        <RunsSkeleton />
      ) : items.length === 0 ? (
        <>
          <div className="dash-empty">
            <h2>{filters.offset > 0 ? 'No more runs' : 'No runs found'}</h2>
            {filters.offset > 0 ? (
              <p className="dash-empty-hint">Return to the previous page to continue browsing.</p>
            ) : hasActiveFilters(filters) ? (
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
          {filters.offset > 0 && paginationFooter}
        </>
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
          {compact ? <div className="run-cards">                {items.map((run) => (
                  <RunRow
                    key={run.thread_id}
                    run={run}
                    onRerun={setRerunThreadId}
                    canRerun={canRerun}
                    onInspect={(threadId) => {
                      setSelectedRunId(threadId)
                      setSelectedInspectorPhase(null)
                    }}
                    inspected={selectedRunId === run.thread_id}
                    selection={
                      compareMode
                        ? {
                            selected: compareSelection.includes(run.thread_id),
                            disabled:
                              !compareSelection.includes(run.thread_id) &&
                              compareSelection.length >= MAX_COMPARE_RUNS,
                            onToggle: toggleCompareSelection,
                          }
                        : undefined
                    }
                  />
                ))}</div> : (
          <div className="data-table-wrap">
            <table className="data-table striped runs-table">
              <thead>
                <tr>
                  {compareMode && (
                    <th className="runs-select-col">
                      <span className="sr-only">Select for compare</span>
                    </th>
                  )}
                  <th>Run</th>
                  <th>Project</th>
                  <th>Applications</th>
                  <th>Progress</th>
                  <th>Created</th>
                  <th>Duration</th>
                  <th>Test result</th>
                  <th>Status</th>
                  <th className="runs-actions-cell">
                    Inspect
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((run) => (
                  <RunRow
                    key={run.thread_id}
                    run={run}
                    onRerun={setRerunThreadId}
                    canRerun={canRerun}
                    onInspect={(threadId) => {
                      setSelectedRunId(threadId)
                      setSelectedInspectorPhase(null)
                    }}
                    inspected={selectedRunId === run.thread_id}
                    selection={
                      compareMode
                        ? {
                            selected: compareSelection.includes(run.thread_id),
                            disabled:
                              !compareSelection.includes(run.thread_id) &&
                              compareSelection.length >= MAX_COMPARE_RUNS,
                            onToggle: toggleCompareSelection,
                          }
                        : undefined
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
          )}
          {selectedRunId && (
            <section className="glass-panel runs-inspector" aria-label="Run inspector">
              <div className="runs-inspector-head">
                <div>
                  <span className="home-section-title">Inline Inspector</span>
                  <h3 className="runs-inspector-title">
                    {inspector.data?.detail.title ?? selectedRunId}
                  </h3>
                </div>
                <Link className="btn btn-secondary btn-sm" to={`/runs/${selectedRunId}`}>
                  Open
                </Link>
              </div>
              {inspector.isPending ? (
                <p className="runs-muted">Loading run details…</p>
              ) : inspector.isError && !inspector.data ? (
                <p className="runs-refresh-error" role="alert">
                  Inspect failed: {errorMessage(inspector.error)}
                </p>
              ) : inspector.data ? (
                <>
                  {inspector.isError && (
                    <CachedDataWarning
                      error={inspector.error}
                      onRetry={() => void inspector.refetch()}
                    />
                  )}
                  {inspector.data.stateParseFailed && (
                    <p role="status">Run state could not be read. Unavailable phase statuses use summary data.</p>
                  )}
                  <div className="runs-inspector-phases">
                    {normalizedPhases(inspector.data.state, inspector.data.detail.phase_strip, inspector.data.stateParseFailed).map(({ phase, status }) => {
                      const visual = pipelinePhaseVisual(status)
                      return (
                        <button
                          key={phase}
                          type="button"
                          className={`runs-phase-button${selectedInspectorPhase === phase ? ' active' : ''}`}
                          onClick={() => setSelectedInspectorPhase(phase)}
                        >
                          <span>{phase.replaceAll('_', ' ')}</span>
                          <span className={`pipeline-status-pill pipeline-status-pill--${visual}`}>
                            {pipelineStatusLabel(status)}
                          </span>
                        </button>
                      )
                    })}
                  </div>
                  {selectedInspectorPhase ? (
                    <div className="runs-inspector-json">
                      <JsonViewer
                        value={inspector.data.state.phase_results?.[selectedInspectorPhase] ?? {}}
                        ariaLabel="Phase output JSON"
                      />
                    </div>
                  ) : null}
                </>
              ) : null}
            </section>
          )}
          {paginationFooter}
        </>
      )}
      {rerunThreadId && (
        <PreflightModal threadId={rerunThreadId} onClose={() => setRerunThreadId(null)} />
      )}
      <CompareSelectBar selected={compareSelection} onClear={() => setCompareSelection([])} />
    </section>
  )
}
