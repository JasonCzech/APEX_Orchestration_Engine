import { GateDecisionContext } from '@/hitl/GateDecisionContext'
import { formatTimestamp } from './runDisplay'
import { ScopeContext } from '@/components/ux/ScopeContext'
import { useRef, useState } from 'react'
import { Link, Navigate, useLocation, useNavigate, useParams } from 'react-router'

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { useCompactLayout } from '@/components/hooks/useCompactLayout'
import { useThreadState } from '@/api/hooks/useThreadState'
import { RequireRole } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { PhaseStrip } from '@/components/runs/PhaseStrip'
import { AbortConfirm } from '@/hitl/GateActionBar'
import { GateModuleView, GateSlimBanner } from '@/hitl/GateModule'
import { useGate } from '@/hitl/useGate'
import { useRunLiveness } from '@/streaming/usePipelineStream'

import { RunOverview } from './RunOverview'
import { LiveStatusChip } from './LiveStatusChip'
import { PhaseWorkspace } from './PhaseWorkspace'
import { lastPlanSelection } from './preflight'
import { OverflowMenu, PreflightModal } from './PreflightModal'
import { PHASE_LABELS, isPhaseName, normalizedPhases, phaseProgress, orchestrationStatusLabel, statusVisual, targetPhaseFor } from './runDisplay'
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
 * - /runs/:threadId -> outcome overview; legacy tab links open the current phase
 * - /runs/:threadId/phases/:phase?tab= -> shared phase flow + phase workspace
 */
export function RunDetailPage() {
  const { threadId = '' } = useParams()
  return <RunDetailContent key={threadId} threadId={threadId} />
}

function RunDetailContent({ threadId }: { threadId: string }) {
  const compact = useCompactLayout()
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
  const phaseStrip = normalizedPhases(state, detail.phase_strip, stateParseFailed)
  if (hitl.gate) {
    const current = phaseStrip.find(entry => entry.phase === hitl.gate?.phase)
    if (current) current.status = hitl.gate.kind === 'prompt_review' ? 'awaiting_prompt_review' : 'awaiting_output_review'
  }
  const progress = phaseProgress(phaseStrip)

  const overview = !phaseParam
  const selectedPhase = phaseParam ?? targetPhaseFor(detail, state)
  if (!isPhaseName(selectedPhase)) return <>
    <ProblemCard title="Unknown phase" message="Open the run overview to choose a valid phase." />
    <Link to={`/runs/${threadId}`}>Open run overview</Link>
  </>
  if (overview && new URLSearchParams(search).has('tab')) {
    return <Navigate to={`/runs/${threadId}/phases/${selectedPhase}${search}`} replace />
  }

  const threadTone = statusVisual(
    detail.thread_status === 'interrupted' ? 'awaiting_output_review' : detail.thread_status,
  )

  // Gate placement (plan 2.a): full module above the workspace tabs when the
  // gate's phase is the selected phase, else a slim banner linking there.
  const gateSlot =
    hitl.gate &&
    (hitl.gate.phase === selectedPhase ? (
      <>
      <GateDecisionContext state={state} phase={hitl.gate.phase} kind={hitl.gate.kind} updatedAt={detail.updated_at} />
      <GateModuleView
        threadId={threadId}
        gate={hitl.gate}
        machineState={hitl.state}
        onEdit={hitl.edit}
        onSubmit={hitl.submit}
        onViewCurrent={hitl.viewCurrent}
      />
      </>
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
        <span role="status" aria-live="polite" className={`status-badge ${threadStatusBadge(detail.thread_status, threadTone.tone)}`}>
          {orchestrationStatusLabel(detail.thread_status, phaseStrip)}
        </span>
        {stateParseFailed && (
          <span
            className="topbar-meta-chip warning"
            title="Some state fields could not be read. Valid fields are retained; unavailable phase statuses use summary data."
          >
            schema drift
          </span>
        )}
        <span className="spacer" />
        <RequireRole role="operator">
          {!hitl.gate && detail.thread_status === 'busy' ? (
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
              onClick={() => setPreflight({ threadId, phases: lastPlanSelection(state.phases_plan) })}
            >
              Re-run selected plan
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
                  label: 'Choose phases…',
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
      <ScopeContext project={detail.project_id} application={detail.app_id} />
      <nav className="ux-actions run-sections" aria-label="Run sections"><Link className="btn btn-secondary" aria-current={overview ? 'page' : undefined} to={`/runs/${threadId}`}>Overview</Link><Link className="btn btn-secondary" aria-current={!overview ? 'page' : undefined} to={`/runs/${threadId}/phases/${selectedPhase}`}>Phases</Link><Link className="btn btn-ghost" to={`/runs/${threadId}/timeline`}>Activity</Link></nav>
      {overview ? <RunOverview detail={detail} state={state} progress={progress} /> : <>
      <section className="run-pipeline-hero glass-panel" aria-label="Pipeline progress">
        <div className="run-pipeline-hero-head">
          <div>
            <div className="run-pipeline-hero-kicker">Pipeline Progress</div>
            <h3 className="run-pipeline-hero-title">Selected phases</h3>
          </div>
          <div className="run-pipeline-counter-group">
            <span className={`run-pipeline-counter${detail.thread_status === 'busy' ? ' live' : ''}`}>
              {progress.completed}/{progress.total}
            </span>
            <LiveStatusChip status={live.stream.status} />
            <span className="ux-context">Run updated {formatTimestamp(detail.updated_at)}</span>
          </div>
        </div>
        <details key={compact ? 'compact' : 'wide'} className="run-phase-navigation" open={!compact}>
        <summary>Phase navigation · {progress.completed}/{progress.total}</summary>
        <PhaseStrip
          strip={phaseStrip}
          size="lg"
          currentPhase={selectedPhase}
          selectedOnly
          onSelect={(phase) => {
            void navigate({ pathname: `/runs/${threadId}/phases/${phase}`, search })
          }}
        />
        </details>
        {phaseStrip.some(entry => entry.status === 'none') && <details className="ux-context"><summary>Phases outside this run</summary><p>{phaseStrip.filter(entry => entry.status === 'none').map(entry => PHASE_LABELS[entry.phase]).join(' · ')}</p></details>}
      </section>
      <div className="run-detail-grid">
        <PhaseWorkspace
          threadId={threadId}
          phase={selectedPhase}
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
      </>}
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
      return 'neutral'
    case 'error':
      return 'danger'
    default:
      return fallback
  }
}
