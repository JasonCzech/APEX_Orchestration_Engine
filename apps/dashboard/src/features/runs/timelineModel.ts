import { PHASE_NAMES, type PipelineState } from '@apex/pipeline-events'

import {
  formatDuration,
  PHASE_LABELS,
  statusLabel,
  statusVisual,
  type StatusTone,
} from './runDisplay'

export interface TimelineEvent {
  at: string
  tone: StatusTone
  label: string
  detail?: string
}

/** Derive the chronological audit ledger from timestamps in the latest snapshot. */
export function deriveTimeline(state: PipelineState): TimelineEvent[] {
  const events: Array<TimelineEvent & { seq: number }> = []
  let seq = 0
  const push = (event: TimelineEvent) => events.push({ ...event, seq: seq++ })

  for (const phase of PHASE_NAMES) {
    const entry = state.phase_results?.[phase]
    if (!entry) continue
    const label = PHASE_LABELS[phase]
    if (entry.started_at) {
      push({
        at: entry.started_at,
        tone: 'accent',
        label: `${label} started`,
        detail: `attempt ${entry.attempt ?? 1}`,
      })
    }
    if (entry.engine_started_at) {
      push({
        at: entry.engine_started_at,
        tone: 'accent',
        label: `Engine started${entry.engine ? ` (${entry.engine})` : ''}`,
        detail: entry.engine_handle?.external_run_id
          ? `run ${entry.engine_handle.external_run_id}`
          : undefined,
      })
    }
    for (const approval of entry.approvals ?? []) {
      if (!approval.at) continue
      push({
        at: approval.at,
        tone: approval.action === 'approve' ? 'success' : 'warning',
        label: `Gate ${approval.gate ?? 'review'}: ${approval.action ?? '—'}`,
        detail: `by ${approval.actor ?? 'unknown'} · ${label}`,
      })
    }
    if (entry.test_summary && entry.ended_at) {
      push({
        at: entry.ended_at,
        tone:
          entry.test_summary.passed === true
            ? 'success'
            : entry.test_summary.passed === false
              ? 'danger'
              : 'neutral',
        label: `Engine summary collected — ${entry.test_summary.passed === true ? 'passed' : entry.test_summary.passed === false ? 'failed' : 'Not available'}`,
        detail: label,
      })
    }
    if (entry.ended_at) {
      push({
        at: entry.ended_at,
        tone: statusVisual(entry.status).tone,
        label: `${label} ${statusLabel(entry.status)}`,
        detail:
          entry.duration_s !== null && entry.duration_s !== undefined
            ? `in ${formatDuration(entry.duration_s)}`
            : undefined,
      })
    }
  }

  return events
    .sort((a, b) => a.at.localeCompare(b.at) || a.seq - b.seq)
    .map(({ at, tone, label, detail }) => ({ at, tone, label, detail }))
}
