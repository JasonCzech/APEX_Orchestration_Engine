import type { PipelineState } from '@apex/pipeline-events'
import { formatTimestamp, isPhaseName, PHASE_LABELS } from '@/features/runs/runDisplay'

/** Consequences use the persisted plan; never assume the default phase sequence. */
export function GateDecisionContext({ state, phase, kind, updatedAt }: {
  state: PipelineState; phase?: string | null; kind?: string | null; updatedAt?: string | null
}) {
  const plan = state.phases_plan
  const index = plan?.findIndex(item => item === phase) ?? -1
  const next = index >= 0 ? plan?.[index + 1] : undefined
  const nextLabel = isPhaseName(next) ? PHASE_LABELS[next] : null
  return <aside className="gate-decision-context">
    <p>Run snapshot updated {formatTimestamp(updatedAt)}. Review the pending output below before deciding.</p>
    {kind === 'phase_review' && <p>{index < 0
      ? 'Approve accepts this phase; the next phase is not available in this snapshot.'
      : nextLabel ? `Approve accepts this phase and continues to ${nextLabel}, subject to its configured reviews.`
        : 'Approve accepts the final selected phase and completes the selected plan.'} Modify &amp; Re-run revises this phase; later phases wait for its review.</p>}
    {kind === 'prompt_review' && <p>Approve uses these prompts to run {isPhaseName(phase) ? PHASE_LABELS[phase] : 'this phase'}. Revisions apply to this phase before execution continues.</p>}
  </aside>
}
