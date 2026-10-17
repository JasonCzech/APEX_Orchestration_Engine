/**
 * Pre-flight modal for phase-subset re-runs (plan Part 2 §4) — the single
 * checkpoint every entry point funnels through (runs-grid row menu,
 * run-detail header split button).
 *
 * Warn-don't-block: blocked rows render as danger and a caption warns that
 * the server will reject at plan resolution, but Start stays enabled — the
 * backend plan_resolver is the authority.
 */
import { useId, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { useThreadState } from '@/api/hooks/useThreadState'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { Dialog } from '@/components/Dialog'

import { assessPlan, lastPlanSelection, type ReadinessRow } from './preflight'
import { PHASE_LABELS } from './runDisplay'
import { useRerun, type GatesMode } from './useRerun'
import './preflight.css'

const GATES_MODES: ReadonlyArray<{ value: GatesMode; label: string }> = [
  { value: 'inherit', label: 'Inherit defaults' },
  { value: 'gated', label: 'All gated' },
  { value: 'auto', label: 'All auto' },
]

export interface PreflightModalProps {
  threadId: string
  /** Pre-checked phases; omitted = the thread's last resolved plan on load. */
  initialSelection?: PhaseName[]
  onClose: () => void
  /** Route owners use this to reject a completion from a no-longer-current thread. */
  isCurrent?: (threadId: string) => boolean
}

export function PreflightModal({
  threadId,
  initialSelection,
  onClose,
  isCurrent = () => true,
}: PreflightModalProps) {
  const navigate = useNavigate()
  const query = useThreadState(threadId)
  const rerun = useRerun()
  const titleId = useId()
  // null until the user toggles (or an explicit initialSelection was given) —
  // grid entry has no thread state on open, so the default selection hydrates
  // from the fetched phases_plan.
  const [touched, setTouched] = useState<PhaseName[] | null>(initialSelection ?? null)
  const [gatesMode, setGatesMode] = useState<GatesMode>('inherit')

  const state = query.data?.state
  const selection = touched ?? lastPlanSelection(state?.phases_plan)
  const assessment = useMemo(
    () => assessPlan(selection, state?.phase_results),
    [selection, state],
  )

  function toggle(phase: PhaseName) {
    // Keep the selection in canonical order on every edit.
    const next = selection.includes(phase)
      ? selection.filter((p) => p !== phase)
      : PHASE_NAMES.filter((p) => p === phase || selection.includes(p))
    setTouched(next)
  }

  const canStart = selection.length > 0 && !rerun.isPending && !query.isPending

  function start() {
    if (!canStart) return
    rerun.mutate(
      {
        threadId,
        phases: selection,
        gatesMode,
      },
      {
        onSuccess: () => {
          if (!isCurrent(threadId)) return
          onClose()
          void navigate(`/runs/${threadId}`)
        },
      },
    )
  }

  function close() {
    if (rerun.isPending) return
    onClose()
  }

  return (
    <Dialog
      overlayClassName="preflight-overlay"
      className="preflight-modal glass-panel"
      onClose={close}
      labelledBy={titleId}
    >
      <h2 className="preflight-title" id={titleId}>
        Re-run phases
      </h2>
      <p className="preflight-caption">
        Prerequisites resolve against phases earlier in this plan or already succeeded on this
        thread.
      </p>

      {query.isPending && !query.data ? (
        <div className="preflight-loading" role="status" aria-label="Loading thread state">
          Loading thread state…
        </div>
      ) : query.isError && !query.data ? (
        <div className="tonal-card danger" role="alert">
          Thread state failed to load:{' '}
          {query.error instanceof Error ? query.error.message : 'unknown error'}
        </div>
      ) : (
        <>
          {query.isError && (
            <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />
          )}
          <div className="preflight-phase-strip" role="group" aria-label="Phases to run">
            {PHASE_NAMES.map((phase) => (
              <button
                key={phase}
                type="button"
                className="preflight-phase-toggle"
                aria-pressed={selection.includes(phase)}
                onClick={() => toggle(phase)}
              >
                {PHASE_LABELS[phase]}
              </button>
            ))}
          </div>

          <ul className="preflight-readiness" aria-label="Plan readiness">
            {assessment.rows.length === 0 ? (
              <li className="preflight-row empty">No phases selected.</li>
            ) : (
              assessment.rows.map((row) => <ReadinessItem key={row.phase} row={row} />)
            )}
          </ul>

          <div className="preflight-gates" role="group" aria-label="Gates mode">
            {GATES_MODES.map(({ value, label }) => (
              <button
                key={value}
                type="button"
                className="preflight-gates-segment"
                aria-pressed={gatesMode === value}
                onClick={() => setGatesMode(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </>
      )}

      {rerun.isError && (
        <div className="tonal-card danger" role="alert">
          Re-run failed: {rerun.error.message}
        </div>
      )}

      <div className="preflight-actions">
        {assessment.hasBlockers && (
          <span className="preflight-blocker-caption">server will reject at plan resolution</span>
        )}
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={close}
          disabled={rerun.isPending}
        >
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={start}
          disabled={!canStart}
        >
          {rerun.isPending ? 'Starting…' : 'Start phases'}
        </button>
      </div>
    </Dialog>
  )
}

function ReadinessItem({ row }: { row: ReadinessRow }) {
  return (
    <li className={`preflight-row ${row.level}`} data-phase={row.phase} data-level={row.level}>
      <span className="preflight-row-phase">{PHASE_LABELS[row.phase]}</span>
      <span className="preflight-row-message">{row.message}</span>
    </li>
  )
}

// Compatibility export while callers move to the shared component.
export { OverflowMenu } from '@/components/ux/OverflowMenu'
