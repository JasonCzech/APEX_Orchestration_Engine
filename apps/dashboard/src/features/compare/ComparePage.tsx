import { useCallback, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { PHASE_NAMES, type PhaseResultEntry, type TestResultSummary } from '@apex/pipeline-events'

import { usePipelines } from '@/api/hooks/usePipelines'
import { useThreadState, type ThreadStateSnapshot } from '@/api/hooks/useThreadState'
import { isApiError } from '@/api/errors'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import type { UseQueryResult } from '@tanstack/react-query'

import {
  PHASE_LABELS,
  formatDuration,
  statusLabel,
  statusVisual,
} from '../runs/runDisplay'
import {
  COMPARE_KPIS,
  MAX_COMPARE_RUNS,
  bestWorst,
  kpiValue,
  parseCompareIds,
  slowestIndex,
  threadStatusTone,
} from './compareModel'
import './compare.css'

const EM_DASH = '—'

interface CompareRun {
  id: string
  query: UseQueryResult<ThreadStateSnapshot, Error>
}

function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return 'This run could not be loaded.'
}

/** <2 ids — explain how to build a selection from /runs. The h2 matches the
 * route handle so shell-level routing tests keyed on "Compare Runs" hold. */
function CompareEmpty({ count }: { count: number }) {
  return (
    <div className="dash-empty">
      <h2>Compare Runs</h2>
      <p className="dash-empty-hint">
        {count === 1
          ? 'One run selected — comparison needs at least two.'
          : 'No runs selected.'}{' '}
        Pick 2–{MAX_COMPARE_RUNS} runs on the runs list (toggle [Compare], tick the rows, then hit
        Compare), or pass ?ids=a,b in the URL.
      </p>
      <Link to="/runs" className="btn btn-primary compare-empty-cta">
        Go to runs
      </Link>
    </div>
  )
}

function CompareSkeleton() {
  return (
    <div
      className="compare-skeleton"
      role="status"
      aria-busy="true"
      aria-label="Loading comparison"
    >
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i} className="glass-panel compare-skeleton-row" />
      ))}
    </div>
  )
}

/** [Add run] — recent runs via usePipelines, excluding already-selected ids.
 * The list query mounts only while the panel is open. */
function AddRunPicker({ ids, onAdd }: { ids: string[]; onAdd: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const full = ids.length >= MAX_COMPARE_RUNS
  return (
    <div className="compare-picker">
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        aria-expanded={open}
        disabled={full}
        title={full ? `Comparison is limited to ${MAX_COMPARE_RUNS} runs` : undefined}
        onClick={() => setOpen((prev) => !prev)}
      >
        Add run
      </button>
      {open && !full && (
        <AddRunPanel
          exclude={ids}
          onAdd={(id) => {
            setOpen(false)
            onAdd(id)
          }}
        />
      )}
    </div>
  )
}

function AddRunPanel({ exclude, onAdd }: { exclude: string[]; onAdd: (id: string) => void }) {
  const query = usePipelines({ limit: 10 })
  const items = (query.data?.items ?? []).filter((run) => !exclude.includes(run.thread_id))
  return (
    <div className="compare-picker-panel glass-panel" aria-label="Add a run to compare">
      <span className="compare-picker-title">Recent runs</span>
      {query.isError && query.data && (
        <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />
      )}
      {query.isPending ? (
        <span className="compare-picker-hint">Loading…</span>
      ) : query.isError && !query.data ? (
        <span className="compare-picker-hint">Recent runs could not be loaded.</span>
      ) : items.length === 0 ? (
        <span className="compare-picker-hint">No other recent runs.</span>
      ) : (
        <ul className="compare-picker-list">
          {items.map((run) => (
            <li key={run.thread_id}>
              <button
                type="button"
                className="compare-picker-item"
                onClick={() => onAdd(run.thread_id)}
              >
                <span className="strong">{run.title || 'Untitled run'}</span>
                <span className="compare-picker-id">{run.thread_id}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** Sticky per-run column header: title (link to the run), status, engine, remove X. */
function ColumnHeader({
  run,
  onRemove,
}: {
  run: CompareRun
  onRemove: (id: string) => void
}) {
  const detail = run.query.data?.detail
  const title = detail?.title || run.id
  return (
    <th scope="col" className="compare-col-head" data-testid={`compare-col-${run.id}`}>
      <div className="compare-col-title-row">
        <Link to={`/runs/${run.id}`} className="compare-col-link">
          <span className="compare-col-title strong">{title}</span>
          <span className="compare-col-id">{run.id}</span>
        </Link>
        <button
          type="button"
          className="btn btn-ghost btn-sm compare-remove"
          aria-label={`Remove ${title} from comparison`}
          onClick={() => onRemove(run.id)}
        >
          ✕
        </button>
      </div>
      <div className="compare-col-meta">
        {run.query.isError && !run.query.data ? (
          <span className="status-badge danger" title={errorMessage(run.query.error)}>
            load failed
          </span>
        ) : (
          <>
            <span className={`status-badge ${threadStatusTone(detail?.thread_status)}`}>
              {detail?.thread_status ?? 'unknown'}
            </span>
            {detail?.engine?.engine && (
              <span
                className="dash-context-chip"
                title={detail.engine.external_run_id ?? undefined}
              >
                {detail.engine.engine}
              </span>
            )}
          </>
        )}
      </div>
    </th>
  )
}

/**
 * /runs/compare?ids=a,b — side-by-side comparison of 2–4 runs (D8, the last
 * placeholder route). One aligned table: a sticky header row of run columns,
 * then a phase strip section (status + duration + attempt, slowest-in-row
 * amber when >1.5x the fastest), an engine-KPI section (best/worst tinting),
 * and an artifacts/warnings count mini-table.
 */
export function ComparePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const ids = useMemo(() => parseCompareIds(searchParams.get('ids')), [searchParams])

  // Fixed hook slots (MAX_COMPARE_RUNS = 4) keep the hook order stable while
  // the id count varies; useThreadState self-disables on undefined. Below the
  // 2-run threshold nothing fetches — the empty state renders instead.
  const activeIds = ids.length >= 2 ? ids : []
  const slot0 = useThreadState(activeIds[0])
  const slot1 = useThreadState(activeIds[1])
  const slot2 = useThreadState(activeIds[2])
  const slot3 = useThreadState(activeIds[3])
  const slots = [slot0, slot1, slot2, slot3]

  const setIds = useCallback(
    (next: string[]) => {
      setSearchParams(next.length > 0 ? { ids: next.join(',') } : {})
    },
    [setSearchParams],
  )

  if (ids.length < 2) return <CompareEmpty count={ids.length} />

  const runs: CompareRun[] = ids.map((id, index) => ({ id, query: slots[index]! }))
  if (runs.some((run) => run.query.isPending)) return <CompareSkeleton />

  const entriesFor = (phase: (typeof PHASE_NAMES)[number]): Array<PhaseResultEntry | undefined> =>
    runs.map((run) => run.query.data?.state.phase_results?.[phase])

  const summaries: Array<TestResultSummary | undefined> = runs.map(
    (run) => run.query.data?.state.phase_results?.['execution']?.test_summary,
  )
  const hasEngineKpis = summaries.some(Boolean)

  const countPhases = PHASE_NAMES.filter((phase) =>
    entriesFor(phase).some((entry) => entry !== undefined),
  )

  return (
    <section className="compare-page animate-enter">
      <header className="compare-toolbar glass-panel">
        <h2 className="compare-title">Compare Runs</h2>
        <span className="compare-caption">{runs.length} runs</span>
        <span className="spacer" />
        <AddRunPicker ids={ids} onAdd={(id) => setIds([...ids, id])} />
        <Link to="/runs" className="btn btn-ghost btn-sm">
          Back to runs
        </Link>
      </header>

      {runs.some((run) => run.query.isError && run.query.data) && (
        <CachedDataWarning
          error={runs.find((run) => run.query.isError && run.query.data)?.query.error}
          onRetry={() => {
            for (const run of runs) {
              if (run.query.isError) void run.query.refetch()
            }
          }}
        />
      )}

      <div className="data-table-wrap compare-table-wrap">
        <table className="data-table compare-table">
          <thead>
            <tr>
              <th scope="col" className="compare-label-col">
                <span className="sr-only">Metric</span>
              </th>
              {runs.map((run) => (
                <ColumnHeader
                  key={run.id}
                  run={run}
                  onRemove={(id) => setIds(ids.filter((existing) => existing !== id))}
                />
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="compare-section-row">
              <th colSpan={runs.length + 1} scope="colgroup">
                Phases
              </th>
            </tr>
            {PHASE_NAMES.map((phase) => {
              const entries = entriesFor(phase)
              const slow = slowestIndex(entries.map((entry) => entry?.duration_s))
              return (
                <tr key={phase}>
                  <th scope="row" className="compare-row-label">
                    {PHASE_LABELS[phase]}
                  </th>
                  {runs.map((run, index) => {
                    const entry = entries[index]
                    return (
                      <td
                        key={run.id}
                        data-testid={`compare-phase-${phase}-${run.id}`}
                        className={slow === index ? 'compare-cell--slow' : undefined}
                      >
                        {entry ? (
                          <div className="compare-phase-cell">
                            <span className={`status-badge ${statusVisual(entry.status).tone}`}>
                              {statusLabel(entry.status)}
                            </span>
                            <span className="compare-duration">
                              {formatDuration(entry.duration_s)}
                            </span>
                            {entry.attempt != null && (
                              <span className="compare-attempt">attempt {entry.attempt}</span>
                            )}
                          </div>
                        ) : (
                          <span className="compare-muted">{EM_DASH}</span>
                        )}
                      </td>
                    )
                  })}
                </tr>
              )
            })}

            {hasEngineKpis && (
              <>
                <tr className="compare-section-row">
                  <th colSpan={runs.length + 1} scope="colgroup">
                    Engine KPIs
                  </th>
                </tr>
                <tr>
                  <th scope="row" className="compare-row-label">
                    Result
                  </th>
                  {runs.map((run, index) => {
                    const summary = summaries[index]
                    return (
                      <td key={run.id} data-testid={`compare-passed-${run.id}`}>
                        {summary ? (
                          <span className={`status-badge ${summary.passed ? 'success' : 'danger'}`}>
                            {summary.passed ? 'passed' : 'failed'}
                          </span>
                        ) : (
                          <span className="compare-muted">{EM_DASH}</span>
                        )}
                      </td>
                    )
                  })}
                </tr>
                {COMPARE_KPIS.map((def) => {
                  const values = summaries.map((summary) => kpiValue(summary, def.key))
                  const ranked = bestWorst(values, def.better)
                  return (
                    <tr key={def.key}>
                      <th scope="row" className="compare-row-label">
                        {def.label}
                      </th>
                      {runs.map((run, index) => {
                        const value = values[index] ?? null
                        const tint =
                          ranked === null
                            ? undefined
                            : ranked.best === index
                              ? 'compare-cell--best'
                              : ranked.worst === index
                                ? 'compare-cell--worst'
                                : undefined
                        return (
                          <td
                            key={run.id}
                            data-testid={`compare-kpi-${def.key}-${run.id}`}
                            className={tint}
                          >
                            {value === null ? (
                              <span className="compare-muted">{EM_DASH}</span>
                            ) : (
                              <span className="compare-kpi-value">{def.format(value)}</span>
                            )}
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </>
            )}

            <tr className="compare-section-row">
              <th colSpan={runs.length + 1} scope="colgroup">
                Artifacts · Warnings
              </th>
            </tr>
            {countPhases.map((phase) => {
              const entries = entriesFor(phase)
              return (
                <tr key={`counts-${phase}`}>
                  <th scope="row" className="compare-row-label">
                    {PHASE_LABELS[phase]}
                  </th>
                  {runs.map((run, index) => {
                    const entry = entries[index]
                    return (
                      <td key={run.id} data-testid={`compare-counts-${phase}-${run.id}`}>
                        {entry ? (
                          <span
                            className="compare-counts"
                            title={`${entry.artifact_ids?.length ?? 0} artifacts, ${entry.warnings?.length ?? 0} warnings`}
                          >
                            {entry.artifact_ids?.length ?? 0} · {entry.warnings?.length ?? 0}
                          </span>
                        ) : (
                          <span className="compare-muted">{EM_DASH}</span>
                        )}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}
