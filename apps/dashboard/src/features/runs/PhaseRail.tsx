import { useState } from 'react'
import { NavLink, useLocation } from 'react-router'

import { PHASE_NAMES, type PhaseName, type PipelineState } from '@apex/pipeline-events'

import { lastPlanSelection, runFromHereSelection } from './preflight'
import { OverflowMenu, PreflightModal } from './PreflightModal'
import {
  formatDuration,
  PHASE_LABELS,
  statusLabel,
  statusVisual,
  TONE_COLOR_VAR,
} from './runDisplay'

/**
 * Left rail: vertical stepper over the 7 canonical phases. Each item links to
 * /runs/:threadId/phases/:phase (current ?tab= is preserved across phases).
 * D4: per-row kebab opens the phase-subset pre-flight modal (re-run this
 * phase / run from here / run phases…).
 */
export function PhaseRail({ threadId, state }: { threadId: string; state: PipelineState }) {
  const { search } = useLocation()
  // Non-null = the pre-flight modal is open with this preselection.
  const [preflight, setPreflight] = useState<PhaseName[] | null>(null)
  const plan = state.phases_plan
  return (
    <nav className="phase-rail glass-panel" aria-label="Pipeline phases">
      <div className="phase-rail-heading">Phases</div>
      {PHASE_NAMES.map((phase) => {
        const entry = state.phase_results?.[phase]
        const status = entry?.status ?? 'pending'
        const { tone, active } = statusVisual(entry?.status)
        const attempt = entry?.attempt ?? 0
        const warningCount = entry?.warnings?.length ?? 0
        const skipped = status === 'skipped'
        return (
          <div className="phase-rail-row" key={phase}>
            <NavLink
              to={{ pathname: `/runs/${threadId}/phases/${phase}`, search }}
              className={({ isActive }) =>
                ['phase-rail-item', isActive ? 'active' : '', skipped ? 'skipped' : '']
                  .filter(Boolean)
                  .join(' ')
              }
              data-phase={phase}
              data-status={status}
            >
              <span
                className={`status-dot${active ? ' live' : ''}`}
                style={{ color: TONE_COLOR_VAR[tone] }}
                aria-hidden="true"
              />
              <span className="phase-rail-body">
                <span className="phase-rail-name">{PHASE_LABELS[phase]}</span>
                <span className="phase-rail-meta">
                  <span className="status-text">{statusLabel(entry?.status)}</span>
                  {entry?.duration_s !== null && entry?.duration_s !== undefined && (
                    <span>{formatDuration(entry.duration_s)}</span>
                  )}
                  {attempt >= 2 && (
                    <span className="attempt-badge" title={`Attempt ${attempt}`}>
                      ×{attempt}
                    </span>
                  )}
                  {warningCount > 0 && (
                    <span
                      className="warning-chip"
                      title={`${warningCount} warning${warningCount === 1 ? '' : 's'}`}
                    >
                      ⚠ {warningCount}
                    </span>
                  )}
                </span>
              </span>
            </NavLink>
            <OverflowMenu
              className="phase-rail-kebab"
              label={`Phase actions: ${PHASE_LABELS[phase]}`}
              items={[
                { label: 'Re-run this phase', onSelect: () => setPreflight([phase]) },
                {
                  label: 'Run from here',
                  onSelect: () => setPreflight(runFromHereSelection(phase, plan)),
                },
                { label: 'Run phases…', onSelect: () => setPreflight(lastPlanSelection(plan)) },
              ]}
            />
          </div>
        )
      })}
      {preflight && (
        <PreflightModal
          threadId={threadId}
          initialSelection={preflight}
          onClose={() => setPreflight(null)}
        />
      )}
    </nav>
  )
}
