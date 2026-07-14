import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import type { PhaseName } from '@apex/pipeline-events'

import type {
  LiveAgentEvent,
  LiveEngineError,
  LiveEngineSample,
  LivePhaseProgress,
  LiveToolCall,
} from './liveTypes'
import { formatTimestamp, PHASE_LABELS, statusLabel, statusVisual, TONE_COLOR_VAR } from './runDisplay'

/**
 * Live activity feed for ONE selected phase (D2, plan flagship screen):
 * - phase_status transitions render as divider rows;
 * - tool_call events render as compact cards;
 * - engine_poll ticks are summarized 1 row / 10 ticks (expandable) so the
 *   high-frequency channel never floods the DOM;
 * - real-agent and retryable engine-error telemetry render as compact cards; reasoning
 *   tokens remain omitted because the durable record is the transcript artifact.
 *
 * The feed derives entries from the streaming layer's flushed view (≤20fps);
 * nothing here touches the react-query cache. Rendered entries are capped at
 * MAX_ENTRIES with an "older truncated" notice (no virtualization dep in D2).
 *
 * Mount with key={phase} — internal bookkeeping is per-phase.
 */

export const ACTIVITY_FEED_MAX_ENTRIES = 500
export const ENGINE_TICKS_PER_ROW = 10
/** px distance from the bottom within which the feed counts as "stuck". */
const STICK_THRESHOLD_PX = 32

interface DividerEntry {
  kind: 'divider'
  key: number
  status: string
  attempt?: number
  at: string
}

interface ToolEntry {
  kind: 'tool'
  key: number
  id: string
  tool: string
  status: string
  at?: string
}

interface AgentEntry {
  kind: 'agent'
  key: number
  type: LiveAgentEvent['type']
  detail: string
  at?: string
}

interface EngineErrorEntry {
  kind: 'engine_error'
  key: number
  detail: string
  at?: string
}

/** tool_call `at` arrives via zod passthrough (typed unknown) — guard it. */
function toolCallAt(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

interface EngineRowEntry {
  kind: 'engine'
  key: number
  fromTick: number
  toTick: number
  samples: LiveEngineSample[]
}

type FeedEntry = DividerEntry | ToolEntry | AgentEntry | EngineErrorEntry | EngineRowEntry

interface FeedState {
  entries: FeedEntry[]
  dropped: number
}

function fmtMetric(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return Number.isInteger(value) ? String(value) : value.toFixed(digits)
}

function fmtPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${Math.round(value)}%`
}

function engineSampleLine(sample: LiveEngineSample): string {
  const stats = sample.live_stats
  const statsText = stats
    ? `vu ${fmtMetric(stats.vusers, 0)} · tps ${fmtMetric(stats.tps)} · err ${
        stats.error_rate === null || stats.error_rate === undefined
          ? '—'
          : `${(stats.error_rate * 100).toFixed(2)}%`
      } · p95 ${fmtMetric(stats.p95_ms, 0)}ms`
    : 'no live stats'
  return `${sample.status ?? '?'} ${fmtPct(sample.progress_pct)} — ${statsText}`
}

export function ActivityFeed({
  phase,
  streamStatus,
  progress,
  toolCalls,
  agentEvents,
  engineErrors,
  engineSamples,
}: {
  phase: PhaseName
  /** LiveStreamStatus from the stream view; drives the empty-state hint only. */
  streamStatus?: string
  /** Latest phase_status for THIS phase (stream.phaseProgress[phase]). */
  progress?: LivePhaseProgress | null
  /** Run-wide accumulated tool calls; filtered to this phase here. */
  toolCalls?: readonly LiveToolCall[] | null
  /** Run-wide real-agent telemetry; filtered to this phase here. */
  agentEvents?: readonly LiveAgentEvent[] | null
  /** Run-wide retryable engine poll failures; filtered to this phase here. */
  engineErrors?: readonly LiveEngineError[] | null
  /** Engine poll ring buffer; only meaningful for the execution phase. */
  engineSamples?: readonly LiveEngineSample[] | null
}) {
  const [feed, setFeed] = useState<FeedState>({ entries: [], dropped: 0 })

  // Per-phase bookkeeping (component is keyed by phase, so refs reset with it).
  const seq = useRef(0)
  const seenTools = useRef(new Set<string>())
  const seenAgentEvents = useRef(new Set<LiveAgentEvent>())
  const seenEngineErrors = useRef(new Set<LiveEngineError>())
  const lastDividerSig = useRef<string | null>(null)
  const prevSampleLen = useRef(0)
  const pendingSamples = useRef<LiveEngineSample[]>([])
  const consumedTicks = useRef(0)

  const progressStatus = progress?.status
  const progressAttempt = progress?.attempt

  useEffect(() => {
    const additions: FeedEntry[] = []

    // 1. phase_status transition -> divider row.
    if (progressStatus) {
      const sig = `${progressStatus}#${progressAttempt ?? 0}`
      if (lastDividerSig.current !== sig) {
        lastDividerSig.current = sig
        additions.push({
          kind: 'divider',
          key: seq.current++,
          status: progressStatus,
          attempt: progressAttempt,
          at: new Date().toISOString(),
        })
      }
    }

    // 2. tool_call events for this phase (id-deduped; the view accumulates run-wide).
    for (const call of toolCalls ?? []) {
      if (call.phase !== phase || seenTools.current.has(call.id)) continue
      seenTools.current.add(call.id)
      additions.push({
        kind: 'tool',
        key: seq.current++,
        id: call.id,
        tool: call.tool,
        status: call.status,
        at: toolCallAt(call.at),
      })
    }

    // 3. Real-agent response/error telemetry. Object identity is stable in the
    // reducer's capped array, so replayed renders do not duplicate cards.
    for (const event of agentEvents ?? []) {
      if (event.phase !== phase || seenAgentEvents.current.has(event)) continue
      seenAgentEvents.current.add(event)
      additions.push({
        kind: 'agent',
        key: seq.current++,
        type: event.type,
        detail:
          event.type === 'agent_error'
            ? event.error
            : `${event.model} produced ${event.chars.toLocaleString()} characters`,
        at: toolCallAt(event.at),
      })
    }

    // 4. Retryable engine poll failures stay visible without being mistaken
    // for schema drift or a terminal phase failure.
    for (const event of engineErrors ?? []) {
      if (event.phase !== phase || seenEngineErrors.current.has(event)) continue
      seenEngineErrors.current.add(event)
      additions.push({
        kind: 'engine_error',
        key: seq.current++,
        detail: `${event.error} · consecutive failure ${event.consecutive_errors} · attempt ${event.attempt}`,
        at: toolCallAt(event.at),
      })
    }

    // 5. engine_poll ticks -> one expandable row per ENGINE_TICKS_PER_ROW.
    if (phase === 'execution' && engineSamples && engineSamples.length > 0) {
      let appended: LiveEngineSample[]
      if (engineSamples.length > prevSampleLen.current) {
        appended = engineSamples.slice(prevSampleLen.current)
      } else if (engineSamples.length < prevSampleLen.current) {
        // Buffer reset (re-run / new attempt): start over from what's there.
        pendingSamples.current = []
        appended = [...engineSamples]
      } else {
        // Saturated ring buffer flushes keep length flat; tick numbering
        // becomes approximate past the 300-sample horizon (acceptable for D2 —
        // the strip chart shows the rolling window, the feed the cadence).
        appended = []
      }
      prevSampleLen.current = engineSamples.length
      pendingSamples.current.push(...appended)
      while (pendingSamples.current.length >= ENGINE_TICKS_PER_ROW) {
        const chunk = pendingSamples.current.splice(0, ENGINE_TICKS_PER_ROW)
        const fromTick = consumedTicks.current + 1
        consumedTicks.current += chunk.length
        additions.push({
          kind: 'engine',
          key: seq.current++,
          fromTick,
          toTick: consumedTicks.current,
          samples: chunk,
        })
      }
    }

    if (additions.length === 0) return
    setFeed((prev) => {
      const entries = [...prev.entries, ...additions]
      const overflow = entries.length - ACTIVITY_FEED_MAX_ENTRIES
      if (overflow > 0) {
        return { entries: entries.slice(overflow), dropped: prev.dropped + overflow }
      }
      return { entries, dropped: prev.dropped }
    })
  }, [
    phase,
    progressStatus,
    progressAttempt,
    toolCalls,
    agentEvents,
    engineErrors,
    engineSamples,
  ])

  // Stick-to-bottom: follow new entries unless the operator scrolled up, then
  // offer a "jump to live" pill (APEX Load live-update indicator pattern).
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const stuckRef = useRef(true)
  const [showJump, setShowJump] = useState(false)

  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (el && stuckRef.current) el.scrollTop = el.scrollHeight
  }, [feed.entries])

  function handleScroll(event: React.UIEvent<HTMLDivElement>) {
    const el = event.currentTarget
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX
    stuckRef.current = atBottom
    setShowJump(!atBottom)
  }

  function jumpToLive() {
    const el = scrollerRef.current
    if (el) el.scrollTop = el.scrollHeight
    stuckRef.current = true
    setShowJump(false)
  }

  if (feed.entries.length === 0) {
    return (
      <div className="dash-empty small" role="tabpanel" aria-label="Activity">
        No live activity for this phase yet.
        <span className="dash-empty-hint">
          {streamStatus === 'live' || streamStatus === 'connecting'
            ? 'Events appear here as the run streams.'
            : 'Activity streams in while the run is executing; the other tabs show the durable snapshot.'}
        </span>
      </div>
    )
  }

  return (
    <div className="activity-feed-wrap" role="tabpanel" aria-label="Activity">
      {feed.dropped > 0 && (
        <div className="activity-truncated-notice" data-testid="activity-truncated">
          {feed.dropped} older {feed.dropped === 1 ? 'entry' : 'entries'} truncated — the timeline
          and transcript artifacts hold the full record.
        </div>
      )}
      <div
        className="activity-feed"
        role="log"
        aria-label={`${PHASE_LABELS[phase]} live activity`}
        ref={scrollerRef}
        onScroll={handleScroll}
        data-testid="activity-feed"
      >
        {feed.entries.map((entry) => {
          if (entry.kind === 'divider') {
            const { tone, active } = statusVisual(entry.status)
            return (
              <div className="activity-divider" key={entry.key} data-testid="activity-divider">
                <span
                  className={`status-dot${active ? ' live' : ''}`}
                  style={{ color: TONE_COLOR_VAR[tone] }}
                  aria-hidden="true"
                />
                <span className="activity-divider-label">
                  {statusLabel(entry.status)}
                  {entry.attempt !== undefined && entry.attempt > 1 ? ` · attempt ${entry.attempt}` : ''}
                </span>
                <span className="activity-divider-rule" aria-hidden="true" />
                <span className="activity-at">{formatTimestamp(entry.at)}</span>
              </div>
            )
          }
          if (entry.kind === 'tool') {
            const tone = entry.status === 'error' ? 'danger' : 'success'
            return (
              <div className="activity-tool-card" key={entry.key} data-testid="activity-tool-card">
                <span className="kind-chip">tool</span>
                <span className="activity-tool-name">{entry.tool}</span>
                <span className={`status-badge ${tone}`}>{entry.status}</span>
                <span className="activity-at">{entry.at ? formatTimestamp(entry.at) : ''}</span>
              </div>
            )
          }
          if (entry.kind === 'agent') {
            const failed = entry.type === 'agent_error'
            return (
              <div className="activity-tool-card" key={entry.key} data-testid="activity-agent-card">
                <span className="kind-chip">agent</span>
                <span className="activity-tool-name">{entry.detail}</span>
                <span className={`status-badge ${failed ? 'danger' : 'success'}`}>
                  {failed ? 'error' : 'response'}
                </span>
                <span className="activity-at">{entry.at ? formatTimestamp(entry.at) : ''}</span>
              </div>
            )
          }
          if (entry.kind === 'engine_error') {
            return (
              <div
                className="activity-tool-card"
                key={entry.key}
                data-testid="activity-engine-error-card"
              >
                <span className="kind-chip">engine</span>
                <span className="activity-tool-name">{entry.detail}</span>
                <span className="status-badge warning">retrying</span>
                <span className="activity-at">{entry.at ? formatTimestamp(entry.at) : ''}</span>
              </div>
            )
          }
          const last = entry.samples[entry.samples.length - 1]
          return (
            <details className="activity-engine-row" key={entry.key} data-testid="activity-engine-row">
              <summary>
                <span className="kind-chip">engine</span>
                <span className="activity-engine-summary">
                  ticks {entry.fromTick}–{entry.toTick}
                  {last ? ` · ${engineSampleLine(last)}` : ''}
                </span>
              </summary>
              <ul className="activity-engine-samples">
                {entry.samples.map((sample, index) => (
                  <li key={index} className="mono">
                    {sample.at ? `${formatTimestamp(sample.at)} · ` : ''}
                    {engineSampleLine(sample)}
                  </li>
                ))}
              </ul>
            </details>
          )
        })}
      </div>
      {showJump && (
        <button type="button" className="jump-to-live-pill" onClick={jumpToLive}>
          Jump to live ↓
        </button>
      )}
    </div>
  )
}
