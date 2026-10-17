import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import { usePipelines, type PipelineSummary } from '@/api/hooks/usePipelines'
import { isApiError } from '@/api/errors'
import { roleAtLeast } from '@/auth/RequireRole'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { useCompactLayout } from '@/components/hooks/useCompactLayout'
import { PhaseStrip } from '@/components/runs/PhaseStrip'
import { ProblemCard } from '@/components/ProblemCard'
import { CopyableId } from '@/components/ux/CopyableId'
import { Pagination } from '@/components/ux/Pagination'
import { TableToolbar } from '@/components/ux/TableToolbar'
import { SortableHeader } from '@/components/ux/SortableHeader'
import { usePreferences, useTimestampPreference } from '@/theme/preferences'
import type { PhaseName } from '@apex/pipeline-events'

import { CompareSelectBar } from '../compare/CompareSelectBar'
import { MAX_COMPARE_RUNS } from '../compare/compareModel'
import { OverflowMenu } from '@/components/ux/OverflowMenu'
import { PreflightModal } from './PreflightModal'
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
  executionResultLabel,
  gateLabel,
  isPhaseName,
  phaseProgressLabel,
  orchestrationStatusLabel,
  orchestrationTone,
  phaseProgress,
  PHASE_LABELS,
  formatDuration,
  formatTimestamp,
} from './runDisplay'
import { RunWorkspace, type RunWorkspaceSection } from './RunDetailPage'
import { runReviewPath } from './reviewPath'
import { sortRunPage, type RunSort, type RunSortKey } from './runsSort'
import './RunsListPage.css'

const SEARCH_DEBOUNCE_MS = 300
const EM_DASH = '—'

function useMasterDetailLayout() {
  const [enabled, setEnabled] = useState(
    () => window.matchMedia?.('(min-width: 1180px)').matches ?? true,
  )
  useEffect(() => {
    const query = window.matchMedia?.('(min-width: 1180px)')
    if (!query) return
    const update = () => setEnabled(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  return enabled
}

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
  onOpen,
  onReview,
  selected,
  selection,
  canRerun,
}: {
  run: PipelineSummary
  onRerun: (threadId: string) => void
  onOpen: (threadId: string, phase?: PhaseName) => void
  onReview: (run: PipelineSummary) => void
  selected: boolean
  selection?: RowSelection
  canRerun: boolean
}) {
  const displayPhase = run.pending_gate?.phase ?? run.current_phase
  const compact = useCompactLayout()
  if (compact) return (
    <article className={`glass-panel run-card${selected ? ' is-selected' : ''}`} data-testid={`runs-row-${run.thread_id}`}>
      <button type="button" className="runs-run-title runs-run-button" aria-label={`Open ${run.title || 'Untitled run'} details`} onClick={() => onOpen(run.thread_id)}>
        {run.title || 'Untitled run'}
      </button>
      <CopyableId value={run.thread_id} label="run ID" />
      <p><strong>{orchestrationStatusLabel(run.thread_status, run.phase_strip)}</strong></p>
      <p>Test result: <strong>{executionResultLabel(run)}</strong></p>
      <p>Progress: {phaseProgressLabel(phaseProgress(run.phase_strip))}</p>
      {run.pending_gate && <button type="button" className="runs-gate-action" aria-label={`${gateLabel(run.pending_gate.kind)} for ${run.title || run.thread_id}${isPhaseName(run.pending_gate.phase) ? `, ${PHASE_LABELS[run.pending_gate.phase]}` : ''}`} onClick={() => onReview(run)}>{gateLabel(run.pending_gate.kind)}</button>}
      {selection && <label><input type="checkbox" checked={selection.selected} disabled={selection.disabled} onChange={() => selection.onToggle(run.thread_id)} /> Compare {run.title}</label>}
      <details><summary>Run information</summary><dl><dt>Project</dt><dd>{run.project_id ?? EM_DASH}</dd><dt>Application</dt><dd>{run.app_id ?? EM_DASH}</dd><dt>Created</dt><dd>{formatTimestamp(run.created_at)}</dd><dt>Creation to last update</dt><dd>{durationLabel(run)}</dd></dl></details>
      <div className="runs-actions-stack"><button className="btn btn-secondary" aria-label={`Open run ${run.title || 'Untitled run'} (${run.thread_id})`} onClick={() => onOpen(run.thread_id)}>Open run</button>{canRerun && <button className="btn btn-ghost" onClick={() => onRerun(run.thread_id)}>Re-run…</button>}</div>
    </article>
  )

  return (
    <tr className={`runs-row${selected ? ' is-selected' : ''}`} data-testid={`runs-row-${run.thread_id}`} aria-selected={selected}>
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
        <button type="button" className="runs-run-link runs-run-button" aria-label={`Open ${run.title || 'Untitled run'} details`} onClick={() => onOpen(run.thread_id)}>
          <span className="runs-run-title strong">{run.title || 'Untitled run'}</span>
          <span className="runs-run-id">{isPhaseName(displayPhase) ? PHASE_LABELS[displayPhase] : 'Phase not recorded'}</span>
        </button>
        <CopyableId value={run.thread_id} label="run ID" />
      </td>
      <td>
        <span className="runs-story-cell">{run.project_id ?? EM_DASH}</span>
      </td>
      <td>
        <span className="runs-application-cell">{run.app_id ?? EM_DASH}</span>
      </td>
      <td>
        <PhaseStrip strip={run.phase_strip} size="sm" />
        <span className="runs-phase-progress">
          {phaseProgressLabel(phaseProgress(run.phase_strip))}
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
            <button
              type="button"
              className="topbar-meta-chip warning runs-gate-chip"
              title={
                run.pending_gate.phase ? `Pending gate on ${run.pending_gate.phase}` : undefined
              }
              aria-label={`${gateLabel(run.pending_gate.kind)} for ${run.title || run.thread_id}${isPhaseName(run.pending_gate.phase) ? `, ${PHASE_LABELS[run.pending_gate.phase]}` : ''}`}
              onClick={() => onReview(run)}
            >
              {gateLabel(run.pending_gate.kind)}
            </button>
          )}
        </div>
      </td>
      <td className="runs-actions-cell">
        <div className="runs-actions-stack">
          <button
            type="button"
            className={`btn btn-sm ${selected ? 'btn-secondary' : 'btn-ghost'}`}
            aria-label={`Open run ${run.title || 'Untitled run'} (${run.thread_id})`}
            aria-pressed={selected}
            onClick={() => onOpen(run.thread_id)}
          >
            Open run
          </button>
          {canRerun && <OverflowMenu
            label={`Run actions: ${run.title || run.thread_id}`}
            items={[
              { label: 'Re-run…', onSelect: () => onRerun(run.thread_id) },
            ]}
          />}
        </div>
      </td>
    </tr>
  )
}

function RunMasterListItem({
  run,
  selected,
  onOpen,
  onReview,
}: {
  run: PipelineSummary
  selected: boolean
  onOpen: (threadId: string, phase?: PhaseName) => void
  onReview: (run: PipelineSummary) => void
}) {
  const phase = isPhaseName(run.pending_gate?.phase) ? run.pending_gate.phase : undefined
  return (
    <button
      type="button"
      className={`runs-master-item${selected ? ' is-selected' : ''}`}
      aria-pressed={selected}
      onClick={() => run.pending_gate ? onReview(run) : onOpen(run.thread_id, phase)}
    >
      <span className="runs-master-item-head">
        <strong>{run.title || 'Untitled run'}</strong>
        <span className={`status-badge ${orchestrationTone(run.thread_status, run.phase_strip)}`}>
          {orchestrationStatusLabel(run.thread_status, run.phase_strip)}
        </span>
      </span>
      <span className="runs-master-item-meta">
        <span>{run.project_id ?? EM_DASH}</span>
        <span>{phaseProgressLabel(phaseProgress(run.phase_strip))}</span>
        <span>{formatTimestamp(run.updated_at)}</span>
      </span>
      <span className={`runs-master-result ${verdictTone(run)}`}>
        Test result: {executionResultLabel(run)}
      </span>
    </button>
  )
}

function RunsSkeleton() {
  return <Skeleton label="Loading runs" rows={6} />
}

/**
 * /runs — history grid (plan UX 2.d). Filters live in the URL (deep-linkable,
 * back/forward safe); the list polls every 15s while the tab is visible and
 * keeps the previous page rendered during transitions.
 */
export function RunsListPage() {
  useTimestampPreference()
  const { preferences } = usePreferences()
  const compact = useCompactLayout()
  const masterDetail = useMasterDetailLayout()
  const navigate = useNavigate()
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
  const selectedRunId = searchParams.get('selected')
  const requestedRunSection = searchParams.get('runSection')
  const selectedRunSection: RunWorkspaceSection =
    requestedRunSection === 'decision' || requestedRunSection === 'overview' || requestedRunSection === 'timeline'
      ? requestedRunSection
      : 'workspace'
  const selectedRunPhase = isPhaseName(searchParams.get('phase')) ? searchParams.get('phase') as PhaseName : null
  const workspaceOpenerRef = useRef<HTMLElement | null>(null)

  const closeRunWorkspace = useCallback(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      for (const key of ['selected', 'runSection', 'phase', 'tab']) next.delete(key)
      return next
    })
    window.requestAnimationFrame(() => workspaceOpenerRef.current?.focus())
  }, [setSearchParams])

  const openRun = useCallback((threadId: string, phase?: PhaseName) => {
    if (!masterDetail) {
      void navigate(phase ? `/runs/${threadId}/phases/${phase}` : `/runs/${threadId}`)
      return
    }
    workspaceOpenerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('selected', threadId)
      next.set('runSection', 'workspace')
      if (phase) next.set('phase', phase)
      else next.delete('phase')
      next.delete('tab')
      return next
    })
  }, [masterDetail, navigate, setSearchParams])

  const openReview = useCallback((run: PipelineSummary) => {
    if (!run.pending_gate) return
    void navigate(
      runReviewPath(run.thread_id, run.pending_gate.interrupt_id, {
        source: 'history',
        phase: isPhaseName(run.pending_gate.phase) ? run.pending_gate.phase : null,
      }),
    )
  }, [navigate])

  const setRunWorkspaceSection = useCallback((section: RunWorkspaceSection, phase?: PhaseName) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('runSection', section)
      if (section === 'workspace' && phase) next.set('phase', phase)
      return next
    }, { replace: true })
  }, [setSearchParams])

  const setRunWorkspacePhase = useCallback((phase: PhaseName) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('runSection', 'workspace')
      next.set('phase', phase)
      return next
    }, { replace: true })
  }, [setSearchParams])

  useEffect(() => {
    if (masterDetail || !selectedRunId) return
    if (selectedRunSection === 'overview') {
      void navigate(`/runs/${selectedRunId}`, { replace: true })
      return
    }
    if (selectedRunSection === 'timeline') {
      const next = new URLSearchParams()
      if (selectedRunPhase) next.set('phase', selectedRunPhase)
      const workspaceTab = searchParams.get('tab')
      if (workspaceTab) next.set('tab', workspaceTab)
      void navigate(
        `/runs/${selectedRunId}/timeline${next.size > 0 ? `?${next}` : ''}`,
        { replace: true },
      )
      return
    }
    void navigate(
      selectedRunPhase
        ? `/runs/${selectedRunId}/phases/${selectedRunPhase}`
        : `/runs/${selectedRunId}?tab=details`,
      { replace: true },
    )
  }, [masterDetail, navigate, searchParams, selectedRunId, selectedRunPhase, selectedRunSection])

  const toggleCompareMode = () => {
    setCompareSelection([])
    const next = !compareMode
    setCompareMode(next)
    if (next && selectedRunId) closeRunWorkspace()
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
        for (const key of ['selected', 'runSection', 'phase', 'tab']) {
          const value = prev.get(key)
          if (value) params.set(key, value)
        }
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

  const [sort, setSort] = useState<RunSort | null>(null)
  const items = useMemo(() => sortRunPage(data?.items ?? [], sort), [data?.items, sort])
  const onSort = (key: RunSortKey) => setSort(current => ({ key, direction: current?.key === key && current.direction === 'asc' ? 'desc' : 'asc' }))
  const total = data?.total
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
    setSearchParams((prev) => {
      const next = new URLSearchParams()
      for (const key of ['selected', 'runSection', 'phase', 'tab']) {
        const value = prev.get(key)
        if (value) next.set(key, value)
      }
      return next
    })
  }

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
        <Pagination label="Runs" offset={filters.offset} limit={filters.limit}
          hasNext={!nextDisabled} pending={isPlaceholderData}
          onChange={({ offset, limit }) => applyFilters({ offset: Math.min(RUNS_MAX_OFFSET, offset), limit })} />
      </footer>
    </>
  )

  return (
    <section className="runs-page animate-enter">
      <TableToolbar label="Run search and actions" filterCount={[filters.q, filters.status, filters.project].filter(Boolean).length} onClear={clearFilters} className="runs-toolbar glass-panel">
        <nav className="ux-actions runs-view-switch" aria-label="Test operations views">
          <Link className="btn btn-secondary" aria-current="page" to="/runs">All runs</Link>
          <Link className="btn btn-secondary" to="/approvals">Needs review</Link>
        </nav>
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
        <button
          type="button"
          className={`btn btn-sm ${compareMode ? 'btn-secondary' : 'btn-ghost'}`}
          aria-pressed={compareMode}
          onClick={toggleCompareMode}
        >
          Compare
        </button>
        {/* D2 minimal launch (live-UI agent); full wizard lands on /runs/new in D4. */}
        <Link to="/runs/new" className="btn btn-primary">New test</Link>
      </TableToolbar>
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
          <StateCard kind={hasActiveFilters(filters) ? 'filtered' : 'empty'} title={filters.offset > 0 ? 'No more runs' : 'No runs found'}
            action={filters.offset > 0 ? null : hasActiveFilters(filters)
              ? <button type="button" className="btn btn-secondary" onClick={clearFilters}>Clear filters</button>
              : <Link to="/runs/new" className="btn btn-primary">Start a new run</Link>}>
            <p>{filters.offset > 0 ? 'Return to the previous page to continue browsing.' : hasActiveFilters(filters) ? 'No runs match the current filters.' : 'Launch your first run to see it here.'}</p>
          </StateCard>
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
          {compact ? (
            <>
              <div className="run-cards">
                {items.map((run) => (
                  <RunRow
                    key={run.thread_id}
                    run={run}
                    onRerun={setRerunThreadId}
                    canRerun={canRerun}
                    onOpen={openRun}
                    onReview={openReview}
                    selected={false}
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
              </div>
              {paginationFooter}
            </>
          ) : selectedRunId && masterDetail ? (
            <div className="runs-master-detail">
              <aside className="runs-master-list glass-panel" aria-label="Run history">
                <div className="runs-master-list-head">
                  <div>
                    <span className="home-section-title">Run history</span>
                    <strong>{rangeCaption}</strong>
                  </div>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={closeRunWorkspace}>
                    Full table
                  </button>
                </div>
                <div className="runs-master-items">
                  {items.map((run) => (
                    <RunMasterListItem
                      key={run.thread_id}
                      run={run}
                      selected={selectedRunId === run.thread_id}
                      onOpen={openRun}
                      onReview={openReview}
                    />
                  ))}
                </div>
                {paginationFooter}
              </aside>
              <section className="runs-workspace-pane" aria-label="Selected run">
                <RunWorkspace
                  key={selectedRunId}
                  threadId={selectedRunId}
                  embedded
                  section={selectedRunSection}
                  phase={selectedRunPhase}
                  onSectionChange={setRunWorkspaceSection}
                  onPhaseChange={setRunWorkspacePhase}
                  onClose={closeRunWorkspace}
                />
              </section>
            </div>
          ) : (
            <>
              <div className="data-table-wrap">
                <table className="data-table striped runs-table">
                  <caption className="ux-context">Sorting applies to this page.</caption>
                  <thead>
                    <tr>
                      {compareMode && (
                        <th className="runs-select-col">
                          <span className="sr-only">Select for compare</span>
                        </th>
                      )}
                      {([
                        ['title', 'Run'], ['project', 'Project'], ['application', 'Application'],
                        ['progress', 'Progress'], ['created', 'Created'], ['elapsed', 'Elapsed snapshot'],
                        ['result', 'Test result'], ['status', 'Status'],
                      ] as const).map(([key, label]) => (
                        <SortableHeader key={key} active={sort?.key === key} direction={sort?.direction ?? 'asc'}
                          label={`Sort this page by ${label}`} onSort={() => onSort(key)}>
                          {key === 'elapsed' ? <span title="Time from creation to last update, including review waits; not execution duration">{label}</span> : label}
                        </SortableHeader>
                      ))}
                      <th className="runs-actions-cell">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((run) => (
                      <RunRow
                        key={run.thread_id}
                        run={run}
                        onRerun={setRerunThreadId}
                        canRerun={canRerun}
                        onOpen={openRun}
                        onReview={openReview}
                        selected={false}
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
              {paginationFooter}
            </>
          )}
        </>
      )}
      {rerunThreadId && (
        <PreflightModal threadId={rerunThreadId} onClose={() => setRerunThreadId(null)} />
      )}
      <CompareSelectBar selected={compareSelection} onClear={() => setCompareSelection([])} />
    </section>
  )
}
