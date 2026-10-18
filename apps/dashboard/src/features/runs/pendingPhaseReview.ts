import type { ArtifactRef, PhaseName, PhaseResultEntry, PhaseReviewPayload, PipelineState } from '@apex/pipeline-events'
import type { GateInstance } from '@/hitl/gateMachine'

/** A display-only view of an unaccepted result. Never write this back to run state. */
export function pendingPhaseReview(phase: PhaseName, state: PipelineState, gate?: GateInstance | null): {
  entry: PhaseResultEntry | undefined
  artifacts: ArtifactRef[]
  pending: PhaseReviewPayload | null
} {
  const durable = state.phase_results?.[phase]
  const pending = gate?.phase === phase && gate.payload?.kind === 'phase_review' ? gate.payload : null
  if (!pending) return { entry: durable, artifacts: state.artifacts ?? [], pending: null }

  // A prior accepted attempt may still occupy the durable slot. Only fall back
  // to its execution evidence when the review identifies the same attempt.
  const evidence = pending.evidence
  const sameAttempt = evidence?.attempt !== undefined && evidence.attempt === durable?.attempt
  const base = sameAttempt ? durable : undefined
  const artifacts = pending.artifacts.flatMap(artifact => artifact.id ? [{
    ...state.artifacts?.find(recorded => recorded.id === artifact.id),
    id: artifact.id,
    ...(artifact.name ? { name: artifact.name } : {}),
    ...(artifact.kind ? { kind: artifact.kind } : {}),
  }] : [])
  const entry: PhaseResultEntry = {
    ...base,
    ...evidence,
    phase,
    // The pending review establishes neither acceptance nor phase completion.
    status: 'awaiting_output_review',
    ended_at: undefined,
    duration_s: evidence?.duration_s,
    summary: pending.summary ?? pending.result_preview.summary ?? evidence?.summary ?? base?.summary,
    reasoning_digest: evidence?.reasoning_digest ?? pending.result_preview.reasoning_digest ?? base?.reasoning_digest,
    artifact_ids: [...new Set(artifacts.map(artifact => artifact.id))],
    warnings: pending.warnings,
    approvals: durable?.approvals,
    projection_truncated: pending.projection_truncated === true || evidence?.projection_truncated === true || base?.projection_truncated === true,
  }
  return { entry, artifacts, pending }
}
