import { executionLink } from '@/features/apex-load/executionLink'
import { Link } from 'react-router'
import type { PipelineState } from '@apex/pipeline-events'
import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { ArtifactLink } from '@/features/artifacts/ArtifactLink'
import { artifactViewerPath } from '@/features/artifacts/artifactPaths'
import {
  executionResultLabel,
  formatDuration,
  executionWasOmitted,
  normalizedPhases,
  orchestrationStatusLabel,
  PHASE_LABELS,
  targetPhaseFor,
  phaseProgress,
} from './runDisplay'

export function RunOverview({ detail, state, progress }: {
  detail: PipelineDetail
  state: PipelineState
  progress: ReturnType<typeof phaseProgress>
}) {
  const execution = state.phase_results?.execution
  const summary = execution?.test_summary
  const executionOmitted = executionWasOmitted(state)
  const assessment = summary?.outcome?.assessment
  const assessmentLabel = assessment && ({ passed: 'Passed', failed: 'Failed', inconclusive: 'Inconclusive', not_assessed: 'Not assessed', unavailable: 'Not available' })[assessment.status]
  const acceptance = assessmentLabel ?? (executionOmitted ? 'Not applicable' : summary
    ? summary.passed === true ? 'Passed' : summary.passed === false ? 'Failed' : 'Not available'
    : executionResultLabel({ execution_result: detail.execution_result }))
  const errors = Object.values(state.phase_results ?? {}).flatMap((entry) => entry.errors ?? [])
  const artifacts = state.artifacts ?? []
  const handle = state.engine_handle ?? execution?.engine_handle
  const external =
    state.external_results && typeof state.external_results === 'object'
      ? (state.external_results as Record<string, unknown>)
      : {}
  const loadUrl = executionLink(
    handle?.console_url ?? handle?.extras?.console_url ?? execution?.source_uri ?? external.uri,
    `/runs/${detail.thread_id}`,
  )
  const phase = targetPhaseFor(detail, state)
  const outcome = state.run_aborted ? 'Stopped by operator'
    : orchestrationStatusLabel(detail.thread_status, normalizedPhases(state, detail.phase_strip, true))
  return (
    <section className="run-overview" aria-label="Run overview">
      <div className="ux-grid">
        <article className="glass-panel ux-card run-outcome-primary">
          <h3>{outcome}</h3>
          <p>
            {progress.completed} of {progress.total} selected phases finished.{' '}
            {execution
              ? 'Execution evidence is summarized separately below.'
              : executionOmitted ? 'Execution was not selected for this run.' : 'Execution evidence has not been recorded.'}
          </p>
          {errors.map((error, i) => (
            <p key={i} role="alert">
              {error}
            </p>
          ))}
          {!errors.length &&
            !state.run_aborted &&
            detail.thread_status === 'idle' &&
            progress.completed !== progress.total && <p>No stop reason was recorded.</p>}
          <Link className="btn btn-primary" to={`/runs/${detail.thread_id}/phases/${phase}`}>
            {detail.pending_gate ? 'Review pending decision' : `Open ${PHASE_LABELS[phase]}`}
          </Link>
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
          <dl>
            <dt>Project</dt>
            <dd>{detail.project_id ?? 'Not recorded'}</dd>
            <dt>Application</dt>
            <dd>{detail.app_id ?? 'Not recorded'}</dd>
            <dt>Load application</dt><dd>{summary?.outcome?.scope?.application_id ?? handle?.application_id ?? handle?.extras?.application_id ?? 'Not recorded'}</dd>
            <dt>Load release</dt><dd>{summary?.outcome?.scope?.release_id ?? handle?.release_id ?? handle?.extras?.release_id ?? 'Not recorded'}</dd>
            <dt>Mapping revision</dt><dd>{handle?.mapping_revision ?? handle?.extras?.mapping_revision ?? 'Not recorded'}</dd>
            <dt>Source</dt>
            <dd>{execution?.provenance ?? 'Not recorded'}</dd>
          </dl>
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
      <article className="glass-panel ux-card">
        <h3>Evidence coverage</h3>
        {summary?.outcome && <p>Load evidence: {summary.outcome.evidence.status}{summary.outcome.evidence.coverage_percent != null ? ` (${summary.outcome.evidence.coverage_percent}%)` : ''}. Assessed {new Date(summary.outcome.observed_at).toLocaleString()}.</p>}
        {(['diagnostics', 'evidence'] as const).map(section => {
          const href = executionLink(handle?.console_url ?? handle?.extras?.console_url ?? execution?.source_uri ?? external.uri, `/runs/${detail.thread_id}`, section)
          return href ? <a key={section} className="btn btn-secondary" href={href} target="_blank" rel="noopener noreferrer">Open Load {section}</a> : null
        })}
        <p className="ux-context">Artifact count describes recorded files, not the completeness of workload evidence.</p>
        <p>
          {artifacts.length
            ? `${artifacts.length} artifacts recorded. Review source evidence before making a release decision.`
            : 'No artifacts recorded yet. Evidence may still be collecting or the run may not have produced any.'}
        </p>
        <div className="ux-actions">
          {artifacts.map((artifact) => (
            <ArtifactLink
              key={artifact.id}
              to={artifactViewerPath(detail.thread_id, artifact.id)}
              returnTo={`/runs/${detail.thread_id}`}
            >
              {artifact.name ?? artifact.kind ?? artifact.id}
            </ArtifactLink>
          ))}
        </div>
      </article>
      <details className="glass-panel ux-card">
        <summary>Technical identifiers</summary>
        <p>{detail.thread_id}</p>
      </details>
    </section>
  )
}
