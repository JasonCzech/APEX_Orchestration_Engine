import { Link, useParams } from 'react-router'

import { PHASE_NAMES, type PipelineState } from '@apex/pipeline-events'

import { useThreadState } from '@/api/hooks/useThreadState'
import { ProblemCard } from '@/components/ProblemCard'

import {
  formatDuration,
  formatTimestamp,
  PHASE_LABELS,
  statusLabel,
  statusVisual,
  TONE_COLOR_VAR,
  type StatusTone,
} from './runDisplay'
import './run-detail.css'

export interface TimelineEvent {
  at: string
  tone: StatusTone
  label: string
  detail?: string
}

/**
 * Derive the chronological ledger from the snapshot. Events only exist where
 * state carries a timestamp: phase started/ended, gate approvals, and engine
 * lifecycle markers (start tick + summary settle on the execution entry).
 * Exported for tests.
 */
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
        tone: entry.test_summary.passed ? 'success' : 'danger',
        label: `Engine summary collected — ${entry.test_summary.passed ? 'passed' : 'failed'}`,
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

/** /runs/:threadId/timeline — chronological audit ledger from the snapshot. */
export function TimelinePage() {
  const { threadId = '' } = useParams()
  const query = useThreadState(threadId)

  if (query.isPending) {
    return (
      <div className="run-detail-skeleton" data-testid="run-detail-skeleton" aria-busy="true">
        <div className="glass-panel skeleton-block" />
      </div>
    )
  }
  if (query.isError) {
    return (
      <ProblemCard
        title="Timeline failed to load"
        message={query.error instanceof Error ? query.error.message : 'Unknown error'}
        onRetry={() => void query.refetch()}
      />
    )
  }

  const { detail, state } = query.data
  const events = deriveTimeline(state)

  return (
    <>
      <header className="run-detail-header">
        <h2 className="run-detail-title">{detail.title ?? detail.thread_id} — timeline</h2>
        <span className="spacer" />
        <Link className="btn btn-ghost btn-sm" to={`/runs/${threadId}`}>
          Back to run
        </Link>
      </header>
      <section className="timeline-panel glass-panel" aria-label="Run timeline">
        {events.length === 0 ? (
          <div className="dash-empty">
            No timeline events yet.
            <span className="dash-empty-hint">Events appear once a phase has run.</span>
          </div>
        ) : (
          <ol className="timeline-list">
            {events.map((event, index) => (
              <li key={index} className="timeline-event">
                <span
                  className="status-dot"
                  style={{ color: TONE_COLOR_VAR[event.tone] }}
                  aria-hidden="true"
                />
                <span className="timeline-at">{formatTimestamp(event.at)}</span>
                <span className="timeline-label">{event.label}</span>
                {event.detail && <span className="timeline-detail">{event.detail}</span>}
              </li>
            ))}
          </ol>
        )}
        <p className="timeline-caption">
          Derived from the latest state snapshot — gate decisions and engine lifecycle gain
          finer-grained entries when live streaming lands in D2.
        </p>
      </section>
    </>
  )
}
