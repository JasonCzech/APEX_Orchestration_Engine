import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import type { PipelineState } from '@apex/pipeline-events'

import { formatTimestamp, TONE_COLOR_VAR } from './runDisplay'
import { deriveTimeline } from './timelineModel'

export function RunTimeline({ state }: { state: PipelineState }) {
  useTimestampPreference()
  const events = deriveTimeline(state)

  return (
    <section className="timeline-panel glass-panel" aria-label="Run timeline">
      <header className="timeline-panel-head">
        <div>
          <span className="run-pipeline-hero-kicker">Run audit trail</span>
          <h3>Timeline</h3>
          <p>Phase activity, review decisions, and execution milestones in chronological order.</p>
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
              <span className="timeline-at">{formatTimestamp(event.at)}</span>
              <span className="timeline-label">{event.label}</span>
              {event.detail && <span className="timeline-detail">{event.detail}</span>}
            </li>
          ))}
        </ol>
      )}
      <p className="timeline-caption">
        Derived from the latest durable run snapshot. Refreshing the run updates this ledger.
      </p>
    </section>
  )
}
