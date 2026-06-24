/**
 * Pre-flight modal for phase-subset re-runs (plan Part 2 §4) — the single
 * checkpoint every entry point funnels through (PhaseRail kebab, runs-grid
 * row menu, run-detail header split button).
 *
 * Warn-don't-block: blocked rows render as danger and a caption warns that
 * the server will reject at plan resolution, but Start stays enabled — the
 * backend plan_resolver is the authority.
 *
 * Also exports OverflowMenu, the small glass dropdown all three entry points
 * share (menu role, Escape/outside-click close, arrow-key focus).
 */
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { useThreadState } from '@/api/hooks/useThreadState'
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
}

export function PreflightModal({ threadId, initialSelection, onClose }: PreflightModalProps) {
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
      { threadId, phases: selection, gatesMode },
      {
        onSuccess: () => {
          onClose()
          void navigate(`/runs/${threadId}?tab=activity`)
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

      {query.isPending ? (
        <div className="preflight-loading" role="status" aria-label="Loading thread state">
          Loading thread state…
        </div>
      ) : query.isError ? (
        <div className="tonal-card danger" role="alert">
          Thread state failed to load:{' '}
          {query.error instanceof Error ? query.error.message : 'unknown error'}
        </div>
      ) : (
        <>
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

/* ── OverflowMenu — shared glass dropdown for the three entry points ─────── */

export interface OverflowMenuItem {
  label: string
  onSelect: () => void
}

export interface OverflowMenuProps {
  /** Accessible name of the trigger button and the menu. */
  label: string
  items: OverflowMenuItem[]
  /** Trigger content; defaults to the kebab glyph. */
  trigger?: ReactNode
  className?: string
}

export function OverflowMenu({ label, items, trigger = '⋯', className = '' }: OverflowMenuProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return undefined
    function onDocMouseDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [open])

  // Focus the first item when the menu opens (menu-button pattern).
  useEffect(() => {
    if (open) listRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()
  }, [open])

  function moveFocus(delta: 1 | -1) {
    const nodes = listRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')
    if (!nodes || nodes.length === 0) return
    const list = Array.from(nodes)
    const index = list.indexOf(document.activeElement as HTMLButtonElement)
    const next = list[(index + delta + list.length) % list.length]
    next?.focus()
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation()
      setOpen(false)
      triggerRef.current?.focus()
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      moveFocus(1)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      moveFocus(-1)
    }
  }

  return (
    <div
      className={`overflow-menu ${className}`.trim()}
      ref={rootRef}
      onKeyDown={onKeyDown}
      // Entry point 2 mounts this inside a clickable grid row — opening the
      // menu or picking an item must never trigger the row navigation.
      onClick={(event) => event.stopPropagation()}
    >
      <button
        type="button"
        ref={triggerRef}
        className="overflow-menu-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        onClick={() => setOpen((value) => !value)}
      >
        {trigger}
      </button>
      {open && (
        <div className="overflow-menu-list glass-panel" role="menu" aria-label={label} ref={listRef}>
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              className="overflow-menu-item"
              onClick={() => {
                setOpen(false)
                item.onSelect()
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
