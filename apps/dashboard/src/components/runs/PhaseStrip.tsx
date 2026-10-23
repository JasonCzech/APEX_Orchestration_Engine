import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import {
  PHASE_LABELS,
  pipelinePhaseVisual,
  pipelineStatusLabel,
} from '@/features/runs/runDisplay'

import './PhaseStrip.css'

/** One backend phase_strip entry (lenient: server sends plain strings). */
export interface PhaseStripSegment {
  phase: string
  status: string
  executor?: string
  completed_by?: string
  attempt?: number | null
}

export interface PhaseStripProps {
  strip: PhaseStripSegment[]
  /** When present, medium/large segments become focusable buttons. Small strips are summaries. */
  onSelect?: (phase: PhaseName) => void
  size?: 'sm' | 'md' | 'lg'
  selectedOnly?: boolean
  currentPhase?: PhaseName | null
}

/** Sentinel for phases absent from the strip (not in this run's plan). */
const NONE = 'none'

const STATUS_MODIFIERS: Record<string, string> = {
  succeeded: 'succeeded',
  failed: 'failed',
  aborted: 'aborted',
  skipped: 'skipped',
  running: 'running',
  awaiting_prompt_review: 'awaiting',
  awaiting_output_review: 'awaiting',
  awaiting_human_completion: 'awaiting',
  pending: 'pending',
  unknown: 'unknown',
  [NONE]: 'none',
}

function segmentModifier(status: string): string {
  return STATUS_MODIFIERS[status] ?? 'unknown'
}

function segmentLabel(phase: PhaseName, status: string, attempt: number | null | undefined, entry?: PhaseStripSegment): string {
  const attemptSuffix = attempt != null ? ` (attempt ${attempt})` : ''
  const owner = entry?.executor ? ` — ${entry.executor === 'hitl' ? 'Person' : 'Agent'}` : ''
  const actor = entry?.completed_by ? ` — completed by ${entry.completed_by}` : ''
  return `${PHASE_LABELS[phase]} — ${pipelineStatusLabel(status)}${attemptSuffix}${owner}${actor}`
}

/**
 * 7-segment phase micro-viz (plan UX 2.d) — one segment per canonical phase,
 * colored by status token. Renders all 7 phases in canonical order regardless
 * of input order; phases missing from the strip render as unknown. Small strips
 * are one labelled graphic; larger strips can expose a button per phase.
 */
export function PhaseStrip({
  strip,
  onSelect,
  size = 'md',
  currentPhase = null,
  selectedOnly = false,
}: PhaseStripProps) {
  const byPhase = new Map(strip.map((entry) => [entry.phase, entry]))

  const selectedPhases = selectedOnly ? PHASE_NAMES.filter(phase => byPhase.get(phase)?.status && byPhase.get(phase)?.status !== NONE) : PHASE_NAMES

  const visiblePhases = selectedPhases.length ? selectedPhases : PHASE_NAMES

  if (size === 'sm') {
    const labels = PHASE_NAMES.map(phase => {
      const entry = byPhase.get(phase)
      return segmentLabel(phase, entry?.status ?? 'unknown', entry?.attempt, entry)
    })
    return <div className="phase-strip phase-strip--sm" role="img" aria-label={`Phase progress. ${labels.join('; ')}`}>
      {PHASE_NAMES.map((phase, index) => <span key={phase} aria-hidden="true"
        className={`phase-seg phase-seg--${segmentModifier(byPhase.get(phase)?.status ?? 'unknown')}`}
        title={labels[index]} />)}
    </div>
  }

  if (size === 'lg') {
    return (
      <div className="phase-strip phase-strip--lg" role="group" aria-label="Phase progress">
        {visiblePhases.map((phase, index) => {
          const entry = byPhase.get(phase)
          const status = entry?.status ?? 'unknown'
          const label = segmentLabel(phase, status, entry?.attempt, entry)
          const visual = pipelinePhaseVisual(status)
          const className = [
            'phase-step',
            `phase-step--${visual}`,
            currentPhase === phase ? 'is-current' : '',
            onSelect ? 'is-actionable' : '',
          ]
            .filter(Boolean)
            .join(' ')

          const content = (
            <>
              <span className="phase-step-circle" aria-hidden="true">
                {({ succeeded: '✓', failed: '✕', aborted: '■', skipped: '–', none: '–', unknown: '?' } as Record<string, string>)[status] ?? index + 1}
              </span>
              <span className="phase-step-copy">
                <span className="phase-step-name">{PHASE_LABELS[phase]}</span>
                {entry?.executor && <span className="phase-step-status">{entry.executor === 'hitl' ? 'Person' : 'Agent'}{entry.completed_by ? ` · ${entry.completed_by}` : ''}</span>}
                <span className="phase-step-status">{status === NONE ? 'Not selected' : pipelineStatusLabel(status)}</span>
              </span>
            </>
          )

          return (
            <div key={phase} className="phase-step-wrap">
              {onSelect ? (
                <button
                  type="button"
                  className={className}
                  title={label}
                  aria-label={label}
                  aria-pressed={currentPhase === phase}
                  onClick={() => onSelect(phase)}
                >
                  {content}
                </button>
              ) : (
                <span role="img" className={className} title={label} aria-label={label}>
                  {content}
                </span>
              )}
              {index < visiblePhases.length - 1 && <span className={`phase-step-connector phase-step-connector--${visual}`} />}
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <div className={`phase-strip phase-strip--${size}`} role="group" aria-label="Phase progress">
      {PHASE_NAMES.map((phase) => {
        const entry = byPhase.get(phase)
        const status = entry?.status ?? 'unknown'
        const label = segmentLabel(phase, status, entry?.attempt, entry)
        const className = `phase-seg phase-seg--${segmentModifier(status)}`

        if (onSelect) {
          return (
            <button
              key={phase}
              type="button"
              className={className}
              title={label}
              aria-label={label}
              aria-pressed={currentPhase === phase}
              onClick={(event) => {
                // Rows in the grid navigate on click; a segment click wins.
                event.stopPropagation()
                onSelect(phase)
              }}
            />
          )
        }
        return <span key={phase} role="img" className={className} title={label} aria-label={label} />
      })}
    </div>
  )
}
