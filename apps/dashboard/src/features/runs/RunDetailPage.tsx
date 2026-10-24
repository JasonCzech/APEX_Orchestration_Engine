import { TestScopeSummary } from '@/features/new-test/TestScopeSummary'
import { CopyTestSetupButton } from './CopyTestSetupButton'
import { SaveRunAsPresetButton } from './RunPreset'
import { useTimestampPreference } from '@/theme/preferences'
import { Skeleton } from '@/components/ux/Skeleton'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
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
import { RunActorNames } from './RunActorIdentity'
import { RunOverview } from './RunOverview'
import { RunTimeline } from './RunTimeline'
import { LiveStatusChip } from './LiveStatusChip'
import { PhaseWorkspace } from './PhaseWorkspace'
import { lastPlanSelection } from './preflight'
import { OverflowMenu } from '@/components/ux/OverflowMenu'
import { PreflightModal } from './PreflightModal'
import { rerunRestriction } from './rerunCapability'
import { RerunGuidance } from './RerunGuidance'
import { PHASE_LABELS, executionResultLabel, formatTimestamp, isPhaseName, normalizedPhases, withLiveRunningPhase, phaseProgress, phaseProgressLabel, orchestrationStatusLabel, orchestrationTone, targetPhaseFor } from './runDisplay'
import { useAbortRun } from './useAbortRun'
import { decisionOutcomeLabel } from '@/hitl/decisionOutcome'
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
  reviewTriage = false,
  queuePosition,
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
  reviewTriage?: boolean
  queuePosition?: { position: number; total: number }
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
  const summaryGateId = query.data?.detail.pending_gate?.interrupt_id
  const snapshotInterrupt = summaryGateId
    ? query.data?.interrupts.find(interrupt => interrupt.interrupt_id === summaryGateId)
    : query.data?.interrupts[0]
  const serverUpdatedAt = query.data?.detail.updated_at
  // LangGraph may reuse an interrupt id. Canonical content/server revisions
  // distinguish new review generations without treating every poll as one.
  const gateGeneration = useMemo(() => snapshotInterrupt
    ? JSON.stringify([serverUpdatedAt ?? null, snapshotInterrupt.payload ?? null]) : undefined,
  [snapshotInterrupt, serverUpdatedAt])
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
  // Run detail confirms its own decisions; the review queue (embedded) keeps
  // its receipt above the queue instead.
  const [receipt, setReceipt] = useState<{ label: string; phase: PhaseName | null; at: string } | null>(null)
  const lastGate = useRef(hitl.gate)
  if (hitl.gate) lastGate.current = hitl.gate
  const accepted = hitl.lastAccepted
  useEffect(() => {
    if (embedded || !accepted || accepted.action === 'discuss' || accepted.action === 'modify') return
    const gate = lastGate.current?.interrupt_id === accepted.interruptId ? lastGate.current : null
    setReceipt({
      label: decisionOutcomeLabel(accepted.action, gate?.kind),
      phase: gate && isPhaseName(gate.phase) ? gate.phase : null,
      at: new Date().toISOString(),
    })
  }, [accepted, embedded])
  // Opening a run that already waits on a decision lands on it; a gate that
  // appears while someone reads the overview must not move them.
  const firstSnapshot = useRef(true)


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
  const rerunBlocked = rerunRestriction(detail.rerun_blocked_reason)
  const phaseStrip = withLiveRunningPhase(normalizedPhases(state, detail.phase_strip, stateParseFailed, detail),
    { ...detail, pending_gate: detail.pending_gate ?? query.data.interrupts[0] ?? null }, live)
  const pendingGate = query.data.interrupts.some(interrupt => interrupt.interrupt_id === hitl.gate?.interrupt_id) ? hitl.gate : null
  const summaryGate = detail.pending_gate
  const announcementGate = summaryGate && typeof summaryGate.kind === 'string' && isPhaseName(summaryGate.phase)
    ? { kind: summaryGate.kind, phase: summaryGate.phase, interrupt_id: summaryGate.interrupt_id }
    : pendingGate
  if (pendingGate?.kind === 'prompt_review' || pendingGate?.kind === 'phase_review' || pendingGate?.kind === 'human_completion') {
    const current = phaseStrip.find(entry => entry.phase === pendingGate.phase)
    if (current) current.status = pendingGate.kind === 'prompt_review' ? 'awaiting_prompt_review' : pendingGate.kind === 'human_completion' ? 'awaiting_human_completion' : 'awaiting_output_review'
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
  // A run waiting on a decision opens on that decision (the gate is pinned
  // above its phase workspace); ?view=overview keeps the overview reachable.
  const gatePhase = detail.pending_gate && isPhaseName(detail.pending_gate.phase) ? detail.pending_gate.phase : null
  const openOnDecision = firstSnapshot.current && !embedded && overview && gatePhase && new URLSearchParams(search).get('view') !== 'overview'
  firstSnapshot.current = false
  if (openOnDecision) {
    return <Navigate to={`/runs/${threadId}/phases/${gatePhase}`} replace />
  }
  // Links back to the overview must not land on a pending decision instead.
  const overviewPath = `/runs/${threadId}${gatePhase || hitl.gate ? '?view=overview' : ''}`

  const logsSearchParams = new URLSearchParams({
    thread: threadId,
    run: detail.title ?? threadId,
    returnTo: `${pathname}${search}`,
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

  const reviewing = !embedded && section === 'workspace' && Boolean(hitl.gate) && hitl.gate?.phase === selectedPhase
  const currentSearchParams = new URLSearchParams(search)
  const workspaceTab = currentSearchParams.get('tab')
  const workspacePath = `/runs/${threadId}/phases/${selectedPhase}${workspaceTab ? `?tab=${encodeURIComponent(workspaceTab)}` : ''}`
  const timelineSearchParams = new URLSearchParams({ phase: selectedPhase })
  if (workspaceTab) timelineSearchParams.set('tab', workspaceTab)
  const timelinePath = `/runs/${threadId}/timeline?${timelineSearchParams}`
  const expandedPath = overview
    ? overviewPath
    : timeline
      ? timelinePath
      : workspacePath

  return (
    <RunActorNames threadId={threadId} revision={detail.updated_at} enabled={Object.values(state.phase_results ?? {}).some(entry => (entry.approvals?.length ?? 0) > 0)}>
    <section className={`run-workspace${embedded ? ' run-workspace--embedded' : ''}`} aria-label="Run workspace">
      <RunAnnouncements threadId={threadId} title={detail.title ?? threadId} phases={phaseStrip} currentPhase={detail.current_phase ?? state.current_phase}
        gate={announcementGate} gateGeneration={gateGeneration} streamStatus={live.stream.status} terminal={['idle', 'error'].includes(detail.thread_status ?? '')}
        triage={reviewTriage} queuePosition={queuePosition} />
      {query.isError && (
        <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />
      )}
      {state.projection_truncated === true && <p role="note" className="tonal-card warning">
        Some recorded evidence was omitted from this snapshot because it exceeds the display limit. The displayed history may be incomplete.
      </p>}
      <header className="run-detail-header">
        <div className="run-detail-summary">
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
        </div>
        {/* Actions keep one position beside the summary, whatever the title length. */}
        <div className="run-detail-actions">
        {(!embedded || showEmbeddedControls) && <RequireRole role="operator">
          <CopyTestSetupButton key={threadId} threadId={threadId} />
          <SaveRunAsPresetButton key={`preset-${threadId}`} threadId={threadId} />
          {!hitl.gate && detail.thread_status === 'busy' ? (
            // No gate to resume through — cancel the active run(s) server-side.
            <AbortConfirm
              key={JSON.stringify([threadId, live.runId ?? 'busy'])}
              disabled={abortRun.isPending}
              onConfirm={() => abortRun.mutate()}
            />
          ) : null}
          {/* A run that failed before its plan resolved has nothing to re-run;
              the overview offers Edit and relaunch instead. */}
          {detail.failure && !progress.planRecorded ? null : rerunBlocked ? <RerunGuidance reason={rerunBlocked} /> : <span className="split-button">
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
          </span>}
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
        </div>
      </header>
      {receipt && (
        <div className="tonal-card success run-decision-receipt" role="status">
          <strong>Decision recorded</strong>
          <span>{receipt.label}{receipt.phase ? ` · ${PHASE_LABELS[receipt.phase]}` : ''} · {formatTimestamp(receipt.at)}</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setReceipt(null)}>Dismiss</button>
        </div>
      )}
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
      <TestScopeSummary value={detail.test_scope} />
      <nav className="run-sections ux-section-navigation" aria-label="Run sections">
        {embedded ? (
          <>
            {hitl.gate && <button type="button" className="ux-section-link" aria-current={decision ? 'page' : undefined} onClick={() => onSectionChange?.('decision', isPhaseName(hitl.gate?.phase) ? hitl.gate.phase : selectedPhase)}>Review</button>}
            <button type="button" className="ux-section-link" aria-current={overview ? 'page' : undefined} onClick={() => onSectionChange?.('overview')}>Overview</button>
            <button type="button" className="ux-section-link" aria-current={section === 'workspace' ? 'page' : undefined} onClick={() => onSectionChange?.('workspace', selectedPhase)}>Workspace</button>
            <button type="button" className="ux-section-link" aria-current={timeline ? 'page' : undefined} onClick={() => onSectionChange?.('timeline')}>Timeline</button>
          </>
        ) : (
          <>
            {hitl.gate && isPhaseName(hitl.gate.phase) && <Link className="ux-section-link" aria-current={reviewing ? 'page' : undefined} to={`/runs/${threadId}/phases/${hitl.gate.phase}`}>Review</Link>}
            <Link className="ux-section-link" aria-current={overview ? 'page' : undefined} to={overviewPath}>Overview</Link>
            <Link className="ux-section-link" aria-current={section === 'workspace' && !reviewing ? 'page' : undefined} to={workspacePath}>Workspace</Link>
            <Link className="ux-section-link" aria-current={timeline ? 'page' : undefined} to={timelinePath}>Timeline</Link>
          </>
        )}
      </nav>
      {overview ? (
        <RunOverview
          detail={detail}
          state={state}
          progress={progress}
          returnTo={embedded ? `${pathname}${search}` : overviewPath}
          onOpenPhase={embedded ? (phase) => onSectionChange?.('workspace', phase) : undefined}
          onReviewGate={embedded && hitl.gate ? () => onSectionChange?.('decision', isPhaseName(hitl.gate?.phase) ? hitl.gate.phase : selectedPhase) : undefined}
        />
      ) : timeline ? (
        <RunTimeline state={state} gate={pendingGate} failure={detail.failure ? { ...detail.failure, at: detail.updated_at } : null} />
      ) : <>
      <section className="run-pipeline-hero glass-panel" aria-label="Test progress">
        <div className="run-pipeline-hero-head">
          <div>
            <div className="run-pipeline-hero-kicker">Test progress</div>
            <h3 className="run-pipeline-hero-title">Workflow phases</h3>
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
          gate={pendingGate}
          gateState={hitl.state}
        />
      </div>
      </>}
    </section>
    </RunActorNames>
  )
}
