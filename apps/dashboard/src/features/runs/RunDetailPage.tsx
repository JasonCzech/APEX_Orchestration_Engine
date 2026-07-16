import { useRef, useState } from 'react'
import { Link, Navigate, useLocation, useNavigate, useParams } from 'react-router'

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { useThreadState } from '@/api/hooks/useThreadState'
import { RequireRole } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { PhaseStrip } from '@/components/runs/PhaseStrip'
import { AbortConfirm } from '@/hitl/GateActionBar'
import { GateModuleView, GateSlimBanner } from '@/hitl/GateModule'
import { useGate } from '@/hitl/useGate'
import { useRunLiveness } from '@/streaming/usePipelineStream'

import { LiveStatusChip } from './LiveStatusChip'
import { PhaseWorkspace } from './PhaseWorkspace'
import { lastPlanSelection } from './preflight'
import { OverflowMenu, PreflightModal } from './PreflightModal'
import { isPhaseName, isPipelinePhaseComplete, statusVisual, targetPhaseFor } from './runDisplay'
import { useAbortRun } from './useAbortRun'
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
 * - /runs/:threadId/phases/:phase?tab= -> shared phase flow + phase workspace
 */
export function RunDetailPage() {
  const { threadId = '' } = useParams()
  return <RunDetailContent key={threadId} threadId={threadId} />
}

function RunDetailContent({ threadId }: { threadId: string }) {
  const { phase: phaseParam } = useParams()
  const { search } = useLocation()
  const navigate = useNavigate()
  const query = useThreadState(threadId)
  // D2 liveness: SSE deltas render on top of the snapshot. The useThreadState
  // poll deliberately stays on while streaming — snapshot is truth, the stream
  // adds liveness (plan: snapshot + tail reconciliation).
  const live = useRunLiveness(threadId)
  // D3 HITL: one page-level gate machine — the workspace GateModule, the slim
  // cross-phase banner, and the header abort all drive the same instance. The
  // stream's pendingGateHint accelerates discovery ahead of the 10s poll.
  const hitl = useGate(threadId, { gateHint: live.stream.pendingGateHint })
  // D4: header Re-run split button — non-null opens the pre-flight modal
  // with this preselection.
  const [preflight, setPreflight] = useState<{
    threadId: string
    phases: PhaseName[]
  } | null>(null)
  const activeThreadIdRef = useRef(threadId)
  activeThreadIdRef.current = threadId
  // Busy-run abort always probes the engine kill switch first; the backend can
  // recover handles that are absent from the compact pipeline summary.
  const abortRun = useAbortRun(threadId)

  if (query.isPending) return <RunDetailSkeleton />
  if (query.isError && !query.data) {
    return (
      <ProblemCard
        title="Run failed to load"
        message={query.error instanceof Error ? query.error.message : 'Unknown error'}
        onRetry={() => void query.refetch()}
      />
    )
  }

  const { detail, state, stateParseFailed } = query.data
  const completedCount = PHASE_NAMES.filter((phase) =>
    isPipelinePhaseComplete(state.phase_results?.[phase]?.status),
  ).length

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
          “{phaseParam}” is not a pipeline phase. Pick a phase from the flow on a valid run page.
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

  // Gate placement (plan 2.a): full module above the workspace tabs when the
  // gate's phase is the selected phase, else a slim banner linking there.
  const gateSlot =
    hitl.gate &&
    (hitl.gate.phase === phaseParam ? (
      <GateModuleView
        threadId={threadId}
        gate={hitl.gate}
        machineState={hitl.state}
        onEdit={hitl.edit}
        onSubmit={hitl.submit}
        onViewCurrent={hitl.viewCurrent}
      />
    ) : (
      <GateSlimBanner threadId={threadId} gate={hitl.gate} />
    ))

  return (
    <>
      {query.isError && (
        <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />
      )}
      <header className="run-detail-header">
        <h2 className="run-detail-title">{detail.title ?? detail.thread_id}</h2>
        <span className={`status-badge ${threadStatusBadge(detail.thread_status, threadTone.tone)}`}>
          {detail.thread_status ?? 'unknown'}
        </span>
        {stateParseFailed && (
          <span
            className="topbar-meta-chip warning"
            title="The state mirror schema rejected this thread's values; rendering the raw snapshot."
          >
            schema drift
          </span>
        )}
        <span className="spacer" />
        <RequireRole role="operator">
          {(hitl.state.tag === 'open' || hitl.state.tag === 'failed') &&
          hitl.gate?.payload?.actions.some((action) => action === 'abort') ? (
            // Header abort drives the SAME machine as the gate action bar
            // (same type-to-confirm arm step, action 'abort').
            <AbortConfirm
              key={JSON.stringify([threadId, hitl.gate?.interrupt_id ?? 'gate'])}
              onConfirm={() => hitl.submit('abort')}
            />
          ) : detail.thread_status === 'busy' ? (
            // No gate to resume through — cancel the active run(s) server-side.
            <AbortConfirm
              key={JSON.stringify([threadId, live.runId ?? 'busy'])}
              disabled={abortRun.isPending}
              onConfirm={() => abortRun.mutate()}
            />
          ) : null}
          <span className="split-button">
            <button
              type="button"
              className="btn btn-secondary btn-sm split-button-main"
              onClick={() => setPreflight({ threadId, phases: [...PHASE_NAMES] })}
            >
              Re-run
            </button>
            <OverflowMenu
              label="Re-run options"
              trigger="▾"
              items={[
                {
                  label: 'All phases',
                  onSelect: () => setPreflight({ threadId, phases: [...PHASE_NAMES] }),
                },
                {
                  label: 'Run phases…',
                  onSelect: () =>
                    setPreflight({ threadId, phases: lastPlanSelection(state.phases_plan) }),
                },
              ]}
            />
          </span>
        </RequireRole>
        <Link className="btn btn-ghost btn-sm" to={`/runs/${threadId}/timeline`}>
          Timeline
        </Link>
        {/* D8 parity: LogsPage's documented ?thread= deep link (logsFilters.ts). */}
        <Link className="btn btn-ghost btn-sm" to={`/logs?thread=${encodeURIComponent(threadId)}`}>
          Logs
        </Link>
      </header>
      {abortRun.isError && (
        <div className="tonal-card danger" role="alert">
          Abort failed: {abortRun.error.message}
        </div>
      )}
      {preflight?.threadId === threadId && (
        <PreflightModal
          key={threadId}
          threadId={threadId}
          initialSelection={preflight.phases}
          isCurrent={(submittedThreadId) => activeThreadIdRef.current === submittedThreadId}
          onClose={() => {
            if (activeThreadIdRef.current === threadId) setPreflight(null)
          }}
        />
      )}
      <section className="run-pipeline-hero glass-panel" aria-label="Pipeline progress">
        <div className="run-pipeline-hero-head">
          <div>
            <div className="run-pipeline-hero-kicker">Pipeline Progress</div>
            <h3 className="run-pipeline-hero-title">7-Phase Orchestration Flow</h3>
          </div>
          <div className="run-pipeline-counter-group">
            <span className={`run-pipeline-counter${detail.thread_status === 'busy' ? ' live' : ''}`}>
              {completedCount}/{PHASE_NAMES.length}
            </span>
            <LiveStatusChip status={live.stream.status} />
          </div>
        </div>
        <PhaseStrip
          strip={detail.phase_strip}
          size="lg"
          currentPhase={phaseParam}
          onSelect={(phase) => {
            void navigate({ pathname: `/runs/${threadId}/phases/${phase}`, search })
          }}
        />
      </section>
      <div className="run-detail-grid">
        <PhaseWorkspace
          threadId={threadId}
          phase={phaseParam}
          state={state}
          stream={live.stream}
          runId={live.runId}
          threadBusy={detail.thread_status === 'busy'}
          gateSlot={gateSlot}
          appId={detail.app_id ?? null}
          gate={hitl.gate}
          gateState={hitl.state}
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
