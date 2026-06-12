import { Link, Navigate, useLocation, useParams } from 'react-router'

import { useThreadState } from '@/api/hooks/useThreadState'
import { ProblemCard } from '@/components/ProblemCard'
import { useRunLiveness } from '@/streaming/usePipelineStream'

import { LiveStatusChip } from './LiveStatusChip'
import { PhaseRail } from './PhaseRail'
import { PhaseWorkspace } from './PhaseWorkspace'
import { RunRail } from './RunRail'
import { isPhaseName, statusVisual, targetPhaseFor } from './runDisplay'
import './run-detail.css'
import './live.css'

function RunDetailSkeleton() {
  return (
    <div className="run-detail-skeleton" data-testid="run-detail-skeleton" aria-busy="true">
      <div className="glass-panel skeleton-block" />
      <div className="glass-panel skeleton-block" />
      <div className="glass-panel skeleton-block short" />
    </div>
  )
}

/**
 * Flagship run-detail read path (D1 snapshot portions).
 *
 * Mounted on BOTH routes:
 * - /runs/:threadId            -> redirects to /phases/:phase (current phase,
 *   else first phase with a result)
 * - /runs/:threadId/phases/:phase?tab= -> three-region layout
 *   (PhaseRail 260px | PhaseWorkspace | RunRail 320px)
 */
export function RunDetailPage() {
  const { threadId = '', phase: phaseParam } = useParams()
  const { search } = useLocation()
  const query = useThreadState(threadId)
  // D2 liveness: SSE deltas render on top of the snapshot. The useThreadState
  // poll deliberately stays on while streaming — snapshot is truth, the stream
  // adds liveness (plan: snapshot + tail reconciliation).
  const live = useRunLiveness(threadId)

  if (query.isPending) return <RunDetailSkeleton />
  if (query.isError) {
    return (
      <ProblemCard
        title="Run failed to load"
        message={query.error instanceof Error ? query.error.message : 'Unknown error'}
        onRetry={() => void query.refetch()}
      />
    )
  }

  const { detail, state, interrupts, stateParseFailed } = query.data

  if (!phaseParam) {
    // Preserve ?tab= etc. so launch deep links (/runs/:id?tab=activity) survive.
    return (
      <Navigate
        to={{ pathname: `/runs/${threadId}/phases/${targetPhaseFor(detail, state)}`, search }}
        replace
      />
    )
  }

  if (!isPhaseName(phaseParam)) {
    return (
      <div className="dash-empty">
        <h2>Unknown phase</h2>
        <p>
          “{phaseParam}” is not a pipeline phase. Pick a phase from the rail on a valid run page.
        </p>
        <Link className="btn btn-secondary btn-sm" to={`/runs/${threadId}`}>
          Back to run
        </Link>
      </div>
    )
  }

  const threadTone = statusVisual(
    detail.thread_status === 'interrupted' ? 'awaiting_output_review' : detail.thread_status,
  )

  return (
    <>
      <header className="run-detail-header">
        <h2 className="run-detail-title">{detail.title ?? detail.thread_id}</h2>
        <span className={`status-badge ${threadStatusBadge(detail.thread_status, threadTone.tone)}`}>
          {detail.thread_status ?? 'unknown'}
        </span>
        <LiveStatusChip status={live.stream.status} />
        {stateParseFailed && (
          <span
            className="topbar-meta-chip warning"
            title="The state mirror schema rejected this thread's values; rendering the raw snapshot."
          >
            schema drift
          </span>
        )}
        <span className="spacer" />
        <Link className="btn btn-ghost btn-sm" to={`/runs/${threadId}/timeline`}>
          Timeline
        </Link>
      </header>
      <div className={`run-detail-grid${interrupts.length > 0 ? ' has-gate' : ''}`}>
        <PhaseRail threadId={threadId} state={state} />
        <PhaseWorkspace
          threadId={threadId}
          phase={phaseParam}
          state={state}
          stream={live.stream}
          threadBusy={detail.thread_status === 'busy'}
        />
        <RunRail
          detail={detail}
          state={state}
          interrupts={interrupts}
          pendingGateHint={live.stream.pendingGateHint}
        />
      </div>
    </>
  )
}

function threadStatusBadge(status: string | null | undefined, fallback: string): string {
  switch (status) {
    case 'busy':
      return 'accent'
    case 'interrupted':
      return 'warning'
    case 'idle':
      return 'success'
    case 'error':
      return 'danger'
    default:
      return fallback
  }
}
