import { useState } from 'react'
import { NavLink, useLocation } from 'react-router'

import { PHASE_NAMES, type PhaseName, type PipelineState } from '@apex/pipeline-events'

import type { PipelineDetail } from '@/api/hooks/useThreadState'

import { lastPlanSelection, runFromHereSelection } from './preflight'
import { OverflowMenu, PreflightModal } from './PreflightModal'
import {
  formatDuration,
  formatTimestamp,
  isPipelinePhaseComplete,
  PHASE_LABELS,
  pipelinePhaseVisual,
  pipelineStatusLabel,
} from './runDisplay'

/**
 * Left rail: vertical stepper over the 7 canonical phases. Each item links to
 * /runs/:threadId/phases/:phase (current ?tab= is preserved across phases).
 * D4: per-row kebab opens the phase-subset pre-flight modal (re-run this
 * phase / run from here / run phases…).
 */
export function PhaseRail({
  threadId,
  detail,
  state,
}: {
  threadId: string
  detail?: PipelineDetail
  state: PipelineState
}) {
  const { search } = useLocation()
  // Non-null = the pre-flight modal is open with this preselection.
  const [preflight, setPreflight] = useState<PhaseName[] | null>(null)
  const plan = state.phases_plan
  const contextCount = state.context_packets?.length ?? 0
  const artifactCount = state.artifacts?.length ?? 0
  const approvalCount = PHASE_NAMES.reduce(
    (count, phase) => count + (state.phase_results?.[phase]?.approvals?.length ?? 0),
    0,
  )

  return (
    <nav className="phase-rail glass-panel" aria-label="Pipeline phases">
      <div className="phase-rail-summary">
        <div className="phase-rail-summary-heading">Pipeline context</div>
        <div className="phase-rail-summary-chips">
          {detail?.project_id && <span className="topbar-meta-chip">{detail.project_id}</span>}
          {detail?.app_id && <span className="topbar-meta-chip accent">{detail.app_id}</span>}
          <span className="topbar-meta-chip">{contextCount} context</span>
          <span className="topbar-meta-chip">{artifactCount} artifacts</span>
        </div>
        <dl className="phase-rail-summary-stats">
          <div>
            <dt>Approvals</dt>
            <dd>{approvalCount}</dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>{formatTimestamp(detail?.updated_at)}</dd>
          </div>
        </dl>
      </div>

      <div className="phase-rail-heading">Phases</div>
      {PHASE_NAMES.map((phase, index) => {
        const entry = state.phase_results?.[phase]
        const status = entry?.status ?? 'pending'
        const visual = pipelinePhaseVisual(status)
        const attempt = entry?.attempt ?? 0
        const warningCount = entry?.warnings?.length ?? 0
        const skipped = status === 'skipped'
        const complete = isPipelinePhaseComplete(status)
        return (
          <div className="phase-rail-row" key={phase}>
            <NavLink
              to={{ pathname: `/runs/${threadId}/phases/${phase}`, search }}
              className={({ isActive }) =>
                [
                  'phase-rail-item',
                  `phase-rail-item--${visual}`,
                  isActive ? 'active' : '',
                  skipped ? 'skipped' : '',
                ]
                  .filter(Boolean)
                  .join(' ')
              }
              data-phase={phase}
              data-status={status}
            >
              <span className={`phase-rail-icon phase-rail-icon--${visual}`} aria-hidden="true">
                {complete ? '✓' : index + 1}
              </span>
              <span className="phase-rail-body">
                <span className="phase-rail-name-row">
                  <span className="phase-rail-name">{PHASE_LABELS[phase]}</span>
                  <span className={`pipeline-status-pill pipeline-status-pill--${visual}`}>
                    {pipelineStatusLabel(status)}
                  </span>
                </span>
                <span className="phase-rail-meta">
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
