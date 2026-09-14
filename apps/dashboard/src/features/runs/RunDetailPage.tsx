import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, Navigate, useLocation, useNavigate, useParams } from 'react-router'

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { useCompactLayout } from '@/components/hooks/useCompactLayout'
import { useThreadState, type GateInterrupt } from '@/api/hooks/useThreadState'
import { RequireRole } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { PhaseStrip } from '@/components/runs/PhaseStrip'
import { ScopeContext } from '@/components/ux/ScopeContext'
import { AbortConfirm } from '@/hitl/GateActionBar'
import { GateDecisionContext } from '@/hitl/GateDecisionContext'
import { GateModuleView, GateSlimBanner } from '@/hitl/GateModule'
import { useGate } from '@/hitl/useGate'
import { useRunLiveness } from '@/streaming/usePipelineStream'

import { RunOverview } from './RunOverview'
import { RunTimeline } from './RunTimeline'
import { LiveStatusChip } from './LiveStatusChip'
import { PhaseWorkspace } from './PhaseWorkspace'
import { lastPlanSelection } from './preflight'
import { OverflowMenu, PreflightModal } from './PreflightModal'
import { runReviewPath } from './reviewPath'
import { PHASE_LABELS, executionResultLabel, formatTimestamp, isPhaseName, normalizedPhases, phaseProgress, orchestrationStatusLabel, statusVisual, targetPhaseFor } from './runDisplay'
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
  const { threadId = '', phase = null } = useParams()
  return (
    <RunWorkspace
      key={threadId}
      threadId={threadId}
      section={phase ? 'workspace' : 'overview'}
      phase={phase}
    />
  )
}

export type RunWorkspaceSection = 'decision' | 'overview' | 'workspace' | 'timeline'

export function RunWorkspace({
  threadId,
  section = 'overview',
  phase: requestedPhase = null,
  embedded = false,
  onSectionChange,
  onPhaseChange,
  onClose,
  renderGate,
  showEmbeddedControls = true,
  waitAge,
}: {
  threadId: string
  section?: RunWorkspaceSection
  phase?: string | null
  embedded?: boolean
  onSectionChange?: (section: RunWorkspaceSection, phase?: PhaseName) => void
  onPhaseChange?: (phase: PhaseName) => void
  onClose?: () => void
  renderGate?: (interrupt: GateInterrupt, returnTo: string) => ReactNode
  showEmbeddedControls?: boolean
  waitAge?: string
}) {
  const compact = useCompactLayout()
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const query = useThreadState(threadId)
  const headingRef = useRef<HTMLHeadingElement>(null)
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

  useEffect(() => {
    if (embedded) headingRef.current?.focus()
  }, [embedded, threadId, query.isPending])

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

  const overview = section === 'overview'
  const timeline = section === 'timeline'
  const decision = section === 'decision'
  const selectedPhase = requestedPhase ?? targetPhaseFor(detail, state)
  if (!isPhaseName(selectedPhase)) return <>
    <ProblemCard title="Unknown phase" message="Open the run overview to choose a valid phase." />
    {embedded ? (
      <button type="button" className="btn btn-secondary" onClick={() => onSectionChange?.('overview')}>
        Open run overview
      </button>
    ) : (
      <Link to={`/runs/${threadId}`}>Open run overview</Link>
    )}
  </>
  if (!embedded && overview && new URLSearchParams(search).has('tab')) {
    return <Navigate to={`/runs/${threadId}/phases/${selectedPhase}${search}`} replace />
  }

  const threadTone = statusVisual(
    detail.thread_status === 'interrupted' ? 'awaiting_output_review' : detail.thread_status,
  )
  const logsSearchParams = new URLSearchParams({
    thread: threadId,
    run: detail.title ?? threadId,
    returnTo: embedded ? `${pathname}${search}` : `/runs/${threadId}`,
  })
  if (detail.created_at) logsSearchParams.set('from', detail.created_at)
  if (detail.updated_at) logsSearchParams.set('to', detail.updated_at)

  // Gate placement (plan 2.a): full module above the workspace tabs when the
  // gate's phase is the selected phase, else a slim banner linking there.
  const gateInterrupt = hitl.gate
    ? query.data.interrupts.find((interrupt) => interrupt.interrupt_id === hitl.gate?.interrupt_id)
    : undefined
  const gateSlot =
    hitl.gate &&
    (hitl.gate.phase === selectedPhase ? (
      <>
      <GateDecisionContext state={state} phase={hitl.gate.phase} kind={hitl.gate.kind} updatedAt={detail.updated_at} />
      {renderGate && gateInterrupt ? renderGate(gateInterrupt, `${pathname}${search}`) : (
        <GateModuleView
          threadId={threadId}
          gate={hitl.gate}
          machineState={hitl.state}
          onEdit={hitl.edit}
          onSubmit={hitl.submit}
          onViewCurrent={hitl.viewCurrent}
          returnTo={embedded ? `${pathname}${search}` : undefined}
        />
      )}
      </>
    ) : (
      <GateSlimBanner
        threadId={threadId}
        gate={hitl.gate}
        onReview={embedded ? () => onSectionChange?.('decision', hitl.gate && isPhaseName(hitl.gate.phase) ? hitl.gate.phase : selectedPhase) : undefined}
      />
    ))

  const currentSearchParams = new URLSearchParams(search)
  const workspaceTab = currentSearchParams.get('tab')
  const workspacePath = `/runs/${threadId}/phases/${selectedPhase}${workspaceTab ? `?tab=${encodeURIComponent(workspaceTab)}` : ''}`
  const timelineSearchParams = new URLSearchParams({ phase: selectedPhase })
  if (workspaceTab) timelineSearchParams.set('tab', workspaceTab)
  const timelinePath = `/runs/${threadId}/timeline?${timelineSearchParams}`
  const expandedPath = overview
    ? `/runs/${threadId}`
    : timeline
      ? timelinePath
      : workspacePath

  return (
    <section className={`run-workspace${embedded ? ' run-workspace--embedded' : ''}`} aria-label="Run workspace">
      {query.isError && (
        <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />
      )}
      <header className="run-detail-header">
        <div className="run-detail-heading">
          {embedded && <span className="run-workspace-kicker">Run workspace</span>}
          <h2 ref={headingRef} tabIndex={embedded ? -1 : undefined} className="run-detail-title">
            {detail.title ?? detail.thread_id}
          </h2>
        </div>
        <span role="status" aria-live="polite" className={`status-badge ${threadStatusBadge(detail.thread_status, threadTone.tone)}`}>
          {orchestrationStatusLabel(detail.thread_status, phaseStrip)}
        </span>
        <span className="run-workspace-meta">Test result: <strong>{executionResultLabel(detail, state)}</strong></span>
        <span className="run-workspace-meta"><strong>{progress.completed}/{progress.total}</strong> phases finished</span>
        {waitAge && <span className="run-workspace-meta">Waiting <strong>{waitAge}</strong></span>}
        {stateParseFailed && (
          <span
            className="topbar-meta-chip warning"
            title="Some state fields could not be read. Valid fields are retained; unavailable phase statuses use summary data."
          >
            schema drift
          </span>
        )}
        <span className="spacer" />
        {(!embedded || showEmbeddedControls) && <RequireRole role="operator">
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
        </RequireRole>}
        <Link
          className="btn btn-ghost btn-sm"
          to={`/logs?${logsSearchParams.toString()}`}
        >
          Logs
        </Link>
        {embedded && showEmbeddedControls && (
          <>
            <Link className="btn btn-ghost btn-sm" to={expandedPath}>
              Expand
            </Link>
            <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} aria-label="Close run workspace">
              Close
            </button>
          </>
        )}
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
      <nav className="ux-actions run-sections" aria-label="Run sections">
        {embedded ? (
          <>
            {hitl.gate && <button type="button" className="btn btn-secondary" aria-current={decision ? 'page' : undefined} onClick={() => onSectionChange?.('decision', isPhaseName(hitl.gate?.phase) ? hitl.gate.phase : selectedPhase)}>Decision</button>}
            <button type="button" className="btn btn-secondary" aria-current={overview ? 'page' : undefined} onClick={() => onSectionChange?.('overview')}>Overview</button>
            <button type="button" className="btn btn-secondary" aria-current={section === 'workspace' ? 'page' : undefined} onClick={() => onSectionChange?.('workspace', selectedPhase)}>Workspace</button>
            <button type="button" className="btn btn-secondary" aria-current={timeline ? 'page' : undefined} onClick={() => onSectionChange?.('timeline')}>Timeline</button>
          </>
        ) : (
          <>
            {hitl.gate && <Link className="btn btn-secondary" aria-current={decision ? 'page' : undefined} to={runReviewPath(threadId, hitl.gate.interrupt_id, { source: 'overview', phase: isPhaseName(hitl.gate.phase) ? hitl.gate.phase : selectedPhase })}>Decision</Link>}
            <Link className="btn btn-secondary" aria-current={overview ? 'page' : undefined} to={`/runs/${threadId}`}>Overview</Link>
            <Link className="btn btn-secondary" aria-current={section === 'workspace' ? 'page' : undefined} to={workspacePath}>Workspace</Link>
            <Link className="btn btn-secondary" aria-current={timeline ? 'page' : undefined} to={timelinePath}>Timeline</Link>
          </>
        )}
      </nav>
      {overview ? (
        <RunOverview
          detail={detail}
          state={state}
          progress={progress}
          returnTo={embedded ? `${pathname}${search}` : undefined}
          onOpenPhase={embedded ? (phase) => onSectionChange?.('workspace', phase) : undefined}
          onReviewGate={embedded && hitl.gate ? () => onSectionChange?.('decision', isPhaseName(hitl.gate?.phase) ? hitl.gate.phase : selectedPhase) : undefined}
        />
      ) : timeline ? (
        <RunTimeline state={state} />
      ) : <>
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
            if (embedded) onPhaseChange?.(phase)
            else void navigate({ pathname: `/runs/${threadId}/phases/${phase}`, search })
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
    </section>
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
