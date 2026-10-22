import { useTimestampPreference } from '@/theme/preferences'
import type { PipelineState } from '@apex/pipeline-events'
import { formatTimestamp, isPhaseName, PHASE_LABELS } from '@/features/runs/runDisplay'
import { KpiComparison } from '@/features/runs/KpiComparison'

/** Consequences use the persisted plan; never assume the default phase sequence. */
export function GateDecisionContext({ state, phase, kind, updatedAt }: {
  state: PipelineState; phase?: string | null; kind?: string | null; updatedAt?: string | null
}) {
  useTimestampPreference()
  const plan = state.phases_plan
  const index = plan?.findIndex(item => item === phase) ?? -1
  const next = index >= 0 ? plan?.[index + 1] : undefined
  const nextLabel = isPhaseName(next) ? PHASE_LABELS[next] : null
  // Execution and the phases that interpret it are judged against the
  // thresholds, so show them beside the output being reviewed.
  const execution = state.phase_results?.execution
  const showKpis = kind === 'phase_review' && execution?.test_summary && ['execution', 'reporting', 'postmortem'].includes(phase ?? '')
  return <section className="gate-decision-context" aria-label="Decision consequences">
    <p>Run snapshot updated {formatTimestamp(updatedAt)}. Review the pending output below before deciding.</p>
    {kind === 'phase_review' && <p>{index < 0
      ? 'Accepting uses this phase output; the next phase is not available in this snapshot.'
      : nextLabel ? `Accepting uses this phase output and continues to ${nextLabel}, subject to its configured reviews.`
        : 'Accepting uses the final selected phase output and completes the selected plan.'} Request changes re-runs this phase; later phases wait for its review.</p>}
    {showKpis && execution.test_summary && <KpiComparison summary={execution.test_summary} thresholds={execution.acceptance_thresholds} caption="Execution thresholds and observed results for this review" />}
    {kind === 'prompt_review' && <p>Execute Phase uses these prompts to run {isPhaseName(phase) ? PHASE_LABELS[phase] : 'this phase'}. Revisions apply to this phase before execution continues. Skipping is irreversible; later phases that require this output cannot run.{nextLabel ? ` The next selected phase is ${nextLabel}.` : ''}</p>}
  </section>
}
