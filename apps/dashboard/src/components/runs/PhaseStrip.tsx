import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import {
  isPipelinePhaseComplete,
  PHASE_LABELS,
  pipelinePhaseVisual,
  pipelineStatusLabel,
} from '@/features/runs/runDisplay'

import './PhaseStrip.css'

/** One backend phase_strip entry (lenient: server sends plain strings). */
export interface PhaseStripSegment {
  phase: string
  status: string
  attempt?: number | null
}

export interface PhaseStripProps {
  strip: PhaseStripSegment[]
  /** When present, segments become focusable buttons. */
  onSelect?: (phase: PhaseName) => void
  size?: 'sm' | 'md' | 'lg'
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
  pending: 'pending',
  [NONE]: 'none',
}

function segmentModifier(status: string): string {
  return STATUS_MODIFIERS[status] ?? 'none'
}

function segmentLabel(phase: PhaseName, status: string, attempt: number | null | undefined): string {
  const attemptSuffix = attempt != null ? ` (attempt ${attempt})` : ''
  return `${phase} — ${status}${attemptSuffix}`
}

/**
 * 7-segment phase micro-viz (plan UX 2.d) — one segment per canonical phase,
 * colored by status token. Renders all 7 phases in canonical order regardless
 * of input order; phases missing from the strip render as "none" (transparent
 * with border). Interactive (button per segment) when onSelect is provided.
 */
export function PhaseStrip({
  strip,
  onSelect,
  size = 'md',
  currentPhase = null,
}: PhaseStripProps) {
  const byPhase = new Map(strip.map((entry) => [entry.phase, entry]))

  if (size === 'lg') {
    return (
      <div className="phase-strip phase-strip--lg" role="group" aria-label="Phase progress">
        {PHASE_NAMES.map((phase, index) => {
          const entry = byPhase.get(phase)
          const status = entry?.status ?? NONE
          const label = segmentLabel(phase, status, entry?.attempt)
          const visual = pipelinePhaseVisual(status)
          const complete = isPipelinePhaseComplete(status)
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
                {complete ? '✓' : index + 1}
              </span>
              <span className="phase-step-copy">
                <span className="phase-step-name">{PHASE_LABELS[phase]}</span>
                <span className="phase-step-status">{pipelineStatusLabel(status)}</span>
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
                  onClick={() => onSelect(phase)}
                >
                  {content}
                </button>
              ) : (
                <span role="img" className={className} title={label} aria-label={label}>
                  {content}
                </span>
              )}
              {index < PHASE_NAMES.length - 1 && <span className={`phase-step-connector phase-step-connector--${visual}`} />}
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
        const status = entry?.status ?? NONE
        const label = segmentLabel(phase, status, entry?.attempt)
        const className = `phase-seg phase-seg--${segmentModifier(status)}`

        if (onSelect) {
          return (
            <button
              key={phase}
              type="button"
              className={className}
              title={label}
              aria-label={label}
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
