import { useTimestampPreference } from '@/theme/preferences'
import { Skeleton } from '@/components/ux/Skeleton'
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

import { RunAnnouncements } from './RunAnnouncements'
import { RunOverview } from './RunOverview'
import { RunTimeline } from './RunTimeline'
import { LiveStatusChip } from './LiveStatusChip'
import { PhaseWorkspace } from './PhaseWorkspace'
import { lastPlanSelection } from './preflight'
import { OverflowMenu } from '@/components/ux/OverflowMenu'
import { PreflightModal } from './PreflightModal'
import { runReviewPath } from './reviewPath'
import { PHASE_LABELS, executionResultLabel, formatTimestamp, isPhaseName, normalizedPhases, phaseProgress, phaseProgressLabel, orchestrationStatusLabel, orchestrationTone, targetPhaseFor } from './runDisplay'
import { useAbortRun } from './useAbortRun'
import './run-detail.css'
import './live.css'

function RunDetailSkeleton() {
  return (
    <Skeleton label="Loading run" rows={3} height={100} data-testid="run-detail-skeleton" />
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
  useTimestampPreference()
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
  useTimestampPreference()
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
    if (embedded && showEmbeddedControls) headingRef.current?.focus()
  }, [embedded, showEmbeddedControls, threadId, query.isPending])

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
  const phaseStrip = normalizedPhases(state, detail.phase_strip, stateParseFailed, detail)
  const pendingGate = query.data.interrupts.some(interrupt => interrupt.interrupt_id === hitl.gate?.interrupt_id) ? hitl.gate : null
  if (pendingGate?.kind === 'prompt_review' || pendingGate?.kind === 'phase_review') {
    const current = phaseStrip.find(entry => entry.phase === pendingGate.phase)
    if (current) current.status = pendingGate.kind === 'prompt_review' ? 'awaiting_prompt_review' : 'awaiting_output_review'
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
    ? query.data.interrupts.find((interrupt) => interrupt.interrupt_id === hitl.gate?.interrupt_id) ?? { interrupt_id: hitl.gate.interrupt_id, kind: hitl.gate.kind, phase: hitl.gate.phase, payload: hitl.gate.payload ?? undefined }
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
      <RunAnnouncements title={detail.title ?? threadId} phases={phaseStrip} gate={pendingGate} streamStatus={live.stream.status} terminal={['idle', 'error'].includes(detail.thread_status ?? '')} />
      {query.isError && (
        <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />
      )}
      {state.projection_truncated === true && <p role="note" className="tonal-card warning">
        Some recorded evidence was omitted from this snapshot because it exceeds the display limit. The displayed history may be incomplete.
      </p>}
      <header className="run-detail-header">
        <div className="run-detail-heading">
          {embedded && <span className="run-workspace-kicker">Run workspace</span>}
          <h2 ref={headingRef} tabIndex={embedded ? -1 : undefined} className="run-detail-title">
            {detail.title ?? detail.thread_id}
          </h2>
        </div>
        <span className={`status-badge ${orchestrationTone(detail.thread_status, phaseStrip)}`}>
          {orchestrationStatusLabel(detail.thread_status, phaseStrip)}
        </span>
        <span className="run-workspace-meta">Test result: <strong>{executionResultLabel(detail, state)}</strong></span>
        <span className="run-workspace-meta">{phaseProgressLabel(progress)}</span>
        {waitAge && <span className="run-workspace-meta">Waiting <strong>{waitAge.endsWith(' ago') ? `for ${waitAge.slice(0, -4)}` : `since ${waitAge}`}</strong></span>}
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
            {hitl.gate && <button type="button" className="btn btn-secondary" aria-current={decision ? 'page' : undefined} onClick={() => onSectionChange?.('decision', isPhaseName(hitl.gate?.phase) ? hitl.gate.phase : selectedPhase)}>Review</button>}
            <button type="button" className="btn btn-secondary" aria-current={overview ? 'page' : undefined} onClick={() => onSectionChange?.('overview')}>Overview</button>
            <button type="button" className="btn btn-secondary" aria-current={section === 'workspace' ? 'page' : undefined} onClick={() => onSectionChange?.('workspace', selectedPhase)}>Workspace</button>
            <button type="button" className="btn btn-secondary" aria-current={timeline ? 'page' : undefined} onClick={() => onSectionChange?.('timeline')}>Timeline</button>
          </>
        ) : (
          <>
            {hitl.gate && <Link className="btn btn-secondary" aria-current={decision ? 'page' : undefined} to={runReviewPath(threadId, hitl.gate.interrupt_id, { source: 'overview', phase: isPhaseName(hitl.gate.phase) ? hitl.gate.phase : selectedPhase })}>Review</Link>}
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
      <section className="run-pipeline-hero glass-panel" aria-label="Test progress">
        <div className="run-pipeline-hero-head">
          <div>
            <div className="run-pipeline-hero-kicker">Test progress</div>
            <h3 className="run-pipeline-hero-title">Selected phases</h3>
          </div>
          <div className="run-pipeline-counter-group">
            <span className={`run-pipeline-counter${detail.thread_status === 'busy' ? ' live' : ''}`}>
              {phaseProgressLabel(progress)}
            </span>
            {['idle', 'error'].includes(detail.thread_status ?? '') ? <span className="ux-context">Recorded snapshot</span> : <LiveStatusChip status={live.stream.status} />}
            <span className="ux-context">Run updated {formatTimestamp(detail.updated_at)}</span>
          </div>
        </div>
        <details key={compact ? 'compact' : 'wide'} className="run-phase-navigation" open={!compact}>
        <summary>Phase navigation · {phaseProgressLabel(progress)}</summary>
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
          terminal={['idle', 'error'].includes(detail.thread_status ?? '')}
          phaseStatus={phaseStrip.find(item => item.phase === selectedPhase)?.status}
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
