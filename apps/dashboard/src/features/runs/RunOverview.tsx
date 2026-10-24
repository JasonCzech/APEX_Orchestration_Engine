import { useTimestampPreference } from '@/theme/preferences'
import { executionLink } from '@/features/apex-load/executionLink'
import { Link } from 'react-router'
import { Fragment } from 'react'
import type { PhaseName, PipelineState } from '@apex/pipeline-events'
import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { ArtifactLink } from '@/features/artifacts/ArtifactLink'
import { artifactViewerPath } from '@/features/artifacts/artifactPaths'
import { ApplicationLabel } from '@/components/ux/ApplicationLabel'
import { CopyableId } from '@/components/ux/CopyableId'
import {
  executionResultLabel,
  isPhaseName,
  formatDuration,
  executionWasOmitted,
  normalizedPhases,
  orchestrationStatusLabel,
  PHASE_LABELS,
  targetPhaseFor,
  phaseProgress,
  phaseProgressLabel,
  formatTimestamp,
} from './runDisplay'
import { runReviewPath } from './reviewPath'
import { CopyTestSetupButton } from './CopyTestSetupButton'
import { RunPresetLine } from './RunPreset'
import { KpiComparison } from './KpiComparison'
import { RequireRole } from '@/auth/RequireRole'

export function RunOverview({ detail, state, progress, onOpenPhase, onReviewGate, returnTo }: {
  detail: PipelineDetail
  state: PipelineState
  progress: ReturnType<typeof phaseProgress>
  onOpenPhase?: (phase: PhaseName) => void
  onReviewGate?: () => void
  returnTo?: string
}) {
  useTimestampPreference()
  const execution = state.phase_results?.execution
  const summary = execution?.status === 'succeeded' ? execution.test_summary : undefined
  const executionOmitted = executionWasOmitted(state)
  const assessment = summary?.outcome?.assessment
  const assessmentLabel = assessment && ({ passed: 'Passed', failed: 'Failed', inconclusive: 'Inconclusive', not_assessed: 'Not assessed', unavailable: 'Not available' })[assessment.status]
  const acceptance = assessmentLabel ?? (executionOmitted ? 'Not available' : summary
    ? summary.passed === true ? 'Passed' : summary.passed === false ? 'Failed' : 'Not available'
    : executionResultLabel(detail, state))
  const errors = Object.values(state.phase_results ?? {}).flatMap((entry) => entry.errors ?? [])
  const artifacts = state.artifacts ?? []
  const incomplete = state.projection_truncated === true
  const missingEvidence = incomplete ? 'Unavailable in this preview' : 'Not recorded'
  const handle = state.engine_handle ?? execution?.engine_handle
  const external =
    state.external_results && typeof state.external_results === 'object'
      ? (state.external_results as Record<string, unknown>)
      : {}
  const loadUrl = executionLink(
    handle?.console_url ?? handle?.extras?.console_url ?? execution?.source_uri ?? external.uri,
    returnTo ?? `/runs/${detail.thread_id}`,
  )
  const optionalContext = [
    ['Load application', summary?.outcome?.scope?.application_id ?? handle?.application_id ?? handle?.extras?.application_id],
    ['Load release', summary?.outcome?.scope?.release_id ?? handle?.release_id ?? handle?.extras?.release_id],
    ['Mapping revision', handle?.mapping_revision ?? handle?.extras?.mapping_revision],
    ['Source', execution?.provenance],
  ] as const
  const absentContext = optionalContext.filter(([, value]) => value == null || value === '')
  const phase = targetPhaseFor(detail, state)
  const outcome = orchestrationStatusLabel(detail.thread_status, normalizedPhases(state, detail.phase_strip, true, detail))
  return (
    <section className="run-overview" aria-label="Run overview">
      <div className="ux-grid">
        <article className="glass-panel ux-card run-outcome-primary">
          <h3>{outcome}</h3>
          <p>
            {phaseProgressLabel(progress)}.{' '}
            {execution
              ? 'Execution evidence is summarized separately below.'
              : executionOmitted ? 'Execution was not selected for this run.'
                : state.external_results || handle
                  ? 'An execution reference is recorded; collected execution evidence is unavailable.'
                  : incomplete ? 'Execution evidence is unavailable in this preview.' : 'Execution evidence has not been recorded.'}
          </p>
          {errors.map((error, i) => (
            <p key={i} role="alert">
              {error}
            </p>
          ))}
          {detail.failure && <p role="status">{detail.failure.message}{detail.failure.error_type && <> <span className="ux-context">({detail.failure.error_type})</span></>}</p>}
          {!errors.length &&
            !detail.failure &&
            !state.run_aborted &&
            ['idle', 'error'].includes(detail.thread_status ?? '') &&
            (detail.thread_status === 'error' || progress.completed !== progress.total) && <p>{incomplete ? 'The failure or stop reason is unavailable in this preview.' : 'No failure or stop reason was recorded.'}</p>}
          {detail.failure && !progress.planRecorded ? (
            // No phase ran, so opening one shows nothing. Relaunching is the recovery.
            <RequireRole role="operator">
              <CopyTestSetupButton threadId={detail.thread_id} label="Edit and relaunch" primary />
            </RequireRole>
          ) : detail.pending_gate && onReviewGate ? (
            <button type="button" className="btn btn-primary" onClick={onReviewGate}>
              Review pending decision
            </button>
          ) : onOpenPhase ? (
            <button type="button" className="btn btn-primary" onClick={() => onOpenPhase(phase)}>
              {detail.pending_gate ? 'Review pending decision' : `Open ${PHASE_LABELS[phase]}`}
            </button>
          ) : (
            <Link
              className="btn btn-primary"
              to={
                detail.pending_gate && isPhaseName(detail.pending_gate.phase)
                  ? `/runs/${detail.thread_id}/phases/${detail.pending_gate.phase}`
                  : detail.pending_gate
                    ? runReviewPath(detail.thread_id, detail.pending_gate.interrupt_id, { source: 'overview' })
                    : `/runs/${detail.thread_id}/phases/${phase}`
              }
            >
              {detail.pending_gate ? 'Review pending decision' : `Open ${PHASE_LABELS[phase]}`}
            </Link>
          )}
        </article>
        <article className="glass-panel ux-card">
          <h3>Test acceptance</h3>
          <strong>{acceptance}</strong>
          <p>
            {assessment?.explanation ?? summary?.notes ??
              (executionOmitted ? 'Execution was not selected; there is no test acceptance decision for this run.' : acceptance === 'Passed'
                ? 'Recorded execution evidence meets the configured acceptance criteria.'
                : acceptance === 'Failed'
                  ? 'Recorded execution evidence does not meet the configured acceptance criteria.'
                  : 'No acceptance assessment is available in this snapshot. Completing orchestration does not certify a release.')}
          </p>
          {summary?.outcome && <p>Load workload: {summary.outcome.execution.termination_reason.replaceAll('_', ' ')}. Planned {formatDuration(summary.outcome.execution.planned_duration_seconds)} · observed {formatDuration(summary.outcome.execution.observed_duration_seconds)}.</p>}
          {summary?.sla_breaches?.map((breach, i) => (
            <p key={i}>{breach}</p>
          ))}
        </article>
        <article className="glass-panel ux-card">
          <h3>Run context</h3>
          <RunPresetLine threadId={detail.thread_id} />
          <dl>
            <dt>Project</dt>
            <dd>{detail.project_id ?? 'Not recorded'}</dd>
            <dt>Application</dt>
            <dd><ApplicationLabel project={detail.project_id} application={detail.app_id} /></dd>
            {optionalContext.filter(([, value]) => value != null && value !== '').map(([label, value]) => <Fragment key={label}><dt>{label}</dt><dd>{value}</dd></Fragment>)}
          </dl>
          {absentContext.length > 0 && <details className="run-context-unrecorded"><summary>Unrecorded context ({absentContext.length})</summary><dl>{absentContext.map(([label]) => <Fragment key={label}><dt>{label}</dt><dd>{missingEvidence}</dd></Fragment>)}</dl></details>}
          {loadUrl && (
            <a
              className="btn btn-secondary"
              href={loadUrl}
              target="_blank"
              rel="noopener noreferrer"
            >
              Open load execution
            </a>
          )}
          <Link
            className="btn btn-ghost"
            to={`/apex-load${detail.project_id ? `?project=${encodeURIComponent(detail.project_id)}` : ''}`}
          >
            Load integration
          </Link>
        </article>
      </div>
      {summary && (Object.keys(summary.kpis ?? {}).length > 0 || execution?.acceptance_thresholds) && (
        <article className="glass-panel ux-card" aria-labelledby="run-kpi-heading">
          <h3 id="run-kpi-heading">Requested vs observed</h3>
          <KpiComparison summary={summary} thresholds={execution?.acceptance_thresholds} />
        </article>
      )}
      <article className="glass-panel ux-card">
        <h3>Evidence coverage</h3>
        {summary?.outcome && <p>Load evidence: {summary.outcome.evidence.status}{summary.outcome.evidence.coverage_percent != null ? ` (${summary.outcome.evidence.coverage_percent}%)` : ''}. Assessed {formatTimestamp(summary.outcome.observed_at)}.</p>}
        {(['diagnostics', 'evidence'] as const).map(section => {
          const href = executionLink(handle?.console_url ?? handle?.extras?.console_url ?? execution?.source_uri ?? external.uri, returnTo ?? `/runs/${detail.thread_id}`, section)
          return href ? <a key={section} className="btn btn-secondary" href={href} target="_blank" rel="noopener noreferrer">Open Load {section}</a> : null
        })}
        <p className="ux-context">Artifact count describes recorded files, not the completeness of workload evidence.</p>
        <p>
          {artifacts.length
            ? `${artifacts.length} artifacts recorded. Review source evidence before making a release decision.`
            : incomplete ? 'Artifact details are unavailable in this preview.' : ['idle', 'error'].includes(detail.thread_status ?? '') ? 'No artifacts were recorded for this run.' : 'No artifacts recorded yet. Evidence may still be collecting.'}
        </p>
        <div className="ux-actions">
          {artifacts.map((artifact) => (
            <ArtifactLink
              key={artifact.id}
              className="run-evidence-artifact-link"
              to={artifactViewerPath(detail.thread_id, artifact.id)}
              returnTo={returnTo ?? `/runs/${detail.thread_id}`}
            >
              {artifact.name ?? artifact.kind ?? artifact.id}
            </ArtifactLink>
          ))}
        </div>
      </article>
      <details className="glass-panel ux-card">
        <summary>Technical identifiers</summary>
        <p>Run <CopyableId value={detail.thread_id} label="run ID" /></p>
        {detail.app_id && <p>Application <CopyableId value={detail.app_id} label="application ID" /></p>}
      </details>
    </section>
  )
}
