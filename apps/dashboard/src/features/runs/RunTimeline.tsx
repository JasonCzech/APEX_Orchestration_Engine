import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import type { GateInstance } from '@/hitl/gateMachine'
import type { PipelineState } from '@apex/pipeline-events'

import { formatTimestamp, TONE_COLOR_VAR } from './runDisplay'
import { deriveTimeline } from './timelineModel'
import { RunActorIdentity } from './RunActorIdentity'

export function RunTimeline({ state, gate, failure }: {
  state: PipelineState
  gate?: GateInstance | null
  /** The graph error that ended the last run; it has no phase event of its own. */
  failure?: { message: string; error_type?: string | null; at?: string | null } | null
}) {
  useTimestampPreference()
  const events = [
    ...deriveTimeline(state, gate),
    ...(failure ? [{ at: failure.at ?? null, tone: 'danger' as const, label: 'Orchestration failed', detail: failure.error_type ? `${failure.message} (${failure.error_type})` : failure.message }] : []),
  ]

  return (
    <section className="timeline-panel glass-panel" aria-label="Run timeline">
      <header className="timeline-panel-head">
        <div>
          <span className="run-pipeline-hero-kicker">Run audit trail</span>
          <h3>Timeline</h3>
          <p>Recorded phase activity, review decisions, and execution milestones, followed by any current review without a recorded timestamp.</p>
        </div>
        <span className="status-badge neutral">
          {events.length} event{events.length === 1 ? '' : 's'}
        </span>
      </header>
      {events.length === 0 ? (
        <StateCard kind="empty" title="No timeline events recorded." action={null}>The snapshot contains no phase events.</StateCard>
      ) : (
        <ol className="timeline-list">
          {events.map((event, index) => (
            <li key={`${event.at}-${event.label}-${index}`} className="timeline-event">
              <span
                className="status-dot"
                style={{ color: TONE_COLOR_VAR[event.tone] }}
                aria-hidden="true"
              />
              <span className="timeline-at">{event.at ? formatTimestamp(event.at) : 'Review time not recorded'}</span>
              <span className="timeline-label">{event.label}</span>
              {(event.detail || event.actor !== undefined) && <span className="timeline-detail">
                {event.actor !== undefined && <><RunActorIdentity id={event.actor} />{event.detail && ' · '}</>}
                {event.detail}
              </span>}
            </li>
          ))}
        </ol>
      )}
      <p className="timeline-caption">
        Derived from the latest durable run snapshot. A current pending review is shown separately from accepted results and has no invented timestamp. Refreshing the run updates this ledger.
      </p>
    </section>
  )
}
