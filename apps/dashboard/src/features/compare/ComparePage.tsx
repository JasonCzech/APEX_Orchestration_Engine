import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { Fragment, useCallback, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { PHASE_NAMES, type PhaseResultEntry, type TestResultSummary } from '@apex/pipeline-events'

import { usePipelines } from '@/api/hooks/usePipelines'
import { useThreadState, type ThreadStateSnapshot } from '@/api/hooks/useThreadState'
import { isApiError } from '@/api/errors'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import type { UseQueryResult } from '@tanstack/react-query'

import {
  PHASE_LABELS,
  normalizedPhases,
  orchestrationStatusLabel,
  orchestrationTone,
  executionResultLabel,
  formatDuration,
  statusLabel,
  statusVisual,
} from '../runs/runDisplay'
import { engineVerdict } from '../runs/acceptanceThresholds'
import {
  COMPARE_KPIS,
  COMPARE_THRESHOLDS,
  type CompareKpiDef,
  completedSummary,
  formatLimit,
  kpiDifference,
  MAX_COMPARE_RUNS,
  bestWorst,
  kpiValue,
  parseCompareIds,
  slowestIndex,
  thresholdCell,
  type ThresholdInput,
  thresholdsDiffer,
  unexplainedSimFailure,
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

/** Explain how to build a comparison from the runs list. */
function CompareEmpty({ count }: { count: number }) {
  return (
    <StateCard kind="empty" title="Choose runs to compare" action={<Link to="/runs" className="btn btn-primary">Go to runs</Link>}>
      <p>{count === 1 ? 'One run selected — comparison needs at least two.' : 'No runs selected.'} Pick 2–{MAX_COMPARE_RUNS} runs on the runs list, then select Compare.</p>
    </StateCard>
  )
}

function CompareSkeleton() {
  return <Skeleton label="Loading comparison" rows={3} />
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

/**
 * One acceptance-threshold row under its KPI row. The simulator's cells say
 * Met/Not met; a real engine's cells name who assessed the limit, and its
 * verdict stays on the Result row.
 */
function ThresholdRow({ thresholdKey, label, def, runs, inputs }: {
  thresholdKey: string
  label: string
  def: CompareKpiDef
  runs: CompareRun[]
  inputs: ThresholdInput[]
}) {
  const minimum = def.better === 'higher'
  const differs = thresholdsDiffer(thresholdKey, inputs)
  return (
    <tr className="compare-threshold-row">
      <th scope="row" className="compare-row-label">
        {label}<small className="compare-difference">{differs ? 'Limits differ' : 'Acceptance limit'}</small>
      </th>
      {runs.map((run, index) => {
        const cell = thresholdCell(thresholdKey, inputs[index]!)
        return (
          <td key={run.id} data-testid={`compare-threshold-${thresholdKey}-${run.id}`}>
            {cell.kind === 'unavailable' ? (
              <span className="compare-muted">{EM_DASH}<small className="compare-difference">No collected result</small></span>
            ) : cell.kind === 'none' ? (
              <span className="compare-muted">None set</span>
            ) : cell.kind === 'engine-native' ? (
              <span className="compare-muted">Set in {cell.engine}</span>
            ) : (
              <div className="compare-phase-cell">
                <span className="compare-kpi-value">{minimum ? '≥' : '≤'} {formatLimit(thresholdKey, cell.limit)}</span>
                {cell.met !== null && (
                  <span className={`status-badge ${cell.met ? 'success' : 'danger'}`}>{cell.met ? 'Met' : 'Not met'}</span>
                )}
                {cell.assessedBy && <small className="compare-difference">Assessed by {cell.assessedBy}</small>}
              </div>
            )}
          </td>
        )
      })}
    </tr>
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
  const snapshot = run.query.data
  const strip = snapshot ? normalizedPhases(snapshot.state, detail?.phase_strip, snapshot.stateParseFailed, detail) : []
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
            <span className={`status-badge ${orchestrationTone(detail?.thread_status, strip)}`}>
              {orchestrationStatusLabel(detail?.thread_status, strip)}
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
 * amber when >1.5x the fastest), an engine-KPI section (best/worst tinting,
 * each KPI followed by its acceptance threshold), and an artifacts/warnings
 * count mini-table.
 */
export function ComparePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [chosenBaseline, setChosenBaseline] = useState<string | null>(null)
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

  const entriesFor = (phase: (typeof PHASE_NAMES)[number]): Array<(Pick<PhaseResultEntry, 'attempt' | 'duration_s' | 'artifact_ids' | 'warnings'> & { status?: string | undefined }) | undefined> =>
    runs.map((run) => {
      const snapshot = run.query.data
      if (!snapshot) return undefined
      const normalized = normalizedPhases(snapshot.state, snapshot.detail.phase_strip, snapshot.stateParseFailed, snapshot.detail).find((entry) => entry.phase === phase)
      if (normalized?.status === 'none') return { status: 'none' }
      return { ...snapshot.state.phase_results?.[phase], status: normalized?.status, attempt: normalized?.attempt ?? undefined }
    })

  const summaries: Array<TestResultSummary | undefined> = runs.map(
    (run) => {
      const execution = run.query.data?.state.phase_results?.execution
      return execution?.status === 'succeeded' ? execution.test_summary : undefined
    },
  )
  const eligibleBaselineIds = ids.filter((_, index) =>
    COMPARE_KPIS.some(def => kpiValue(summaries[index], def.key) !== null),
  )
  const baselineId = eligibleBaselineIds.includes(chosenBaseline ?? '') ? chosenBaseline : eligibleBaselineIds[0]
  const baselineIndex = ids.indexOf(baselineId ?? '')
  // Failed-verdict runs are judged against their limits but not ranked:
  // KPI ranking and the baseline stay limited to succeeded executions.
  const completed = runs.map((run) => completedSummary(run.query.data?.state.phase_results?.execution))
  const hasEngineKpis = completed.some(Boolean)
  const thresholdInputs: ThresholdInput[] = runs.map((run, index) => {
    const execution = run.query.data?.state.phase_results?.execution
    return { summary: execution?.test_summary, thresholds: execution?.acceptance_thresholds, collected: completed[index] }
  })
  const hasThresholdRows = thresholdInputs.some((input) => input.summary)
  const anyThresholds = thresholdInputs.some((input) =>
    Object.keys(COMPARE_THRESHOLDS).some((key) => input.thresholds?.[key] !== undefined),
  )

  const countPhases = PHASE_NAMES.filter((phase) =>
    entriesFor(phase).some((entry) => entry !== undefined),
  )

  return (
    <section className="compare-page animate-enter">
      <header className="compare-toolbar glass-panel">
        <span className="compare-caption">{runs.length} runs</span>
        <label>Baseline <select aria-label="Comparison baseline" value={baselineId ?? ''} disabled={eligibleBaselineIds.length === 0} onChange={(event) => setChosenBaseline(event.target.value)}>{eligibleBaselineIds.length === 0 && <option value="">No collected result</option>}{runs.map((run) => <option key={run.id} value={run.id} disabled={!eligibleBaselineIds.includes(run.id)}>{run.query.data?.detail.title ?? run.id}{eligibleBaselineIds.includes(run.id) ? '' : completed[ids.indexOf(run.id)] ? ' (execution failed)' : ' (no collected result)'}</option>)}</select></label>
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

      {runs.some((run) => run.query.data?.stateParseFailed) && (
        <p role="status">Some run state could not be read. Unavailable phase statuses use summary data for those runs.</p>
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
                Phases · elapsed time includes review waits
              </th>
            </tr>
            {PHASE_NAMES.map((phase) => {
              const entries = entriesFor(phase)
              const slow = slowestIndex(entries.map((entry) => ['succeeded', 'failed'].includes(entry?.status ?? '') ? entry?.duration_s : null))
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

            {(
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
                    const snapshot = run.query.data
                    const failedVerdict = summaries[index] ? undefined : completed[index]
                    const result = !snapshot ? 'Not available' : failedVerdict ? engineVerdict(failedVerdict) : executionResultLabel(snapshot.detail, snapshot.state)
                    return (
                      <td key={run.id} data-testid={`compare-passed-${run.id}`}>
                        <span className={`status-badge ${result === 'Passed' ? 'success' : result === 'Failed' ? 'danger' : 'neutral'}`}>{result}</span>
                        {result === 'Failed' && unexplainedSimFailure(thresholdInputs[index]!) && (
                          <small className="compare-difference" title={completed[index]?.sla_breaches?.join('; ') || undefined}>
                            Not explained by thresholds
                          </small>
                        )}
                      </td>
                    )
                  })}
                </tr>
                {hasThresholdRows && !anyThresholds && (
                  <tr className="compare-threshold-row">
                    <th scope="row" className="compare-row-label">
                      Acceptance thresholds
                    </th>
                    {runs.map((run, index) => {
                      // No run set any limit, so any key yields unavailable, none or engine-native.
                      const cell = thresholdCell('p95_ms', thresholdInputs[index]!)
                      return (
                        <td key={run.id} data-testid={`compare-thresholds-${run.id}`}>
                          {cell.kind === 'unavailable' ? (
                            <span className="compare-muted">{EM_DASH}<small className="compare-difference">No collected result</small></span>
                          ) : (
                            <span className="compare-muted">{cell.kind === 'engine-native' ? `Set in ${cell.engine}` : 'None set'}</span>
                          )}
                        </td>
                      )
                    })}
                  </tr>
                )}
                {COMPARE_KPIS.map((def) => {
                  const values = summaries.map((summary) => kpiValue(summary, def.key))
                  const ranked = bestWorst(values, def.better)
                  const thresholdLabel = COMPARE_THRESHOLDS[def.key]
                  return (
                    <Fragment key={def.key}>
                    {hasEngineKpis && <tr>
                      <th scope="row" className="compare-row-label">
                        {def.label}<small className="compare-difference">{def.better === 'higher' ? 'Higher' : 'Lower'} is better</small>
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
                            {value === null && kpiValue(completed[index], def.key) !== null ? (
                              <><span className="compare-kpi-value">{def.format(kpiValue(completed[index], def.key)!)}</span><small className="compare-difference">Not ranked · execution failed</small></>
                            ) : value === null ? (
                              <span className="compare-muted">{EM_DASH}<small className="compare-difference">No collected result</small></span>
                            ) : (
                              <><span className="compare-kpi-value">{def.format(value)}</span><small className="compare-difference">{index === baselineIndex ? 'Baseline' : kpiDifference(value, values[baselineIndex] ?? null, def)}</small></>
                            )}
                          </td>
                        )
                      })}
                    </tr>}
                    {hasThresholdRows && anyThresholds && thresholdLabel && (
                      <ThresholdRow thresholdKey={def.key} label={thresholdLabel} def={def} runs={runs} inputs={thresholdInputs} />
                    )}
                    </Fragment>
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
                        {entry && entry.status !== 'none' && entry.status !== 'unknown' ? (
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
