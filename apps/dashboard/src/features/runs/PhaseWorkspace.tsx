import type { ReactNode } from 'react'

import { Link, useSearchParams } from 'react-router'

import type {
  ApprovalRecord,
  ArtifactRef,
  PhaseName,
  PhaseResultEntry,
  PipelineState,
  TestResultSummary,
  ToolCallRecord,
} from '@apex/pipeline-events'

import { CodeViewer } from '@/components/viewers/CodeViewer'
import type { GateInstance, GateMachineState } from '@/hitl/gateMachine'

import { ActivityFeed } from './ActivityFeed'
import { EngineStrip } from './EngineStrip'
import type { LiveStreamViewLike } from './liveTypes'
import { PromptReviewSection } from './PromptReviewSection'
import { formatTimestamp, PHASE_LABELS } from './runDisplay'

const TABS = ['details', 'log', 'reasoning'] as const
type WorkspaceTab = (typeof TABS)[number]

const TAB_LABELS: Record<WorkspaceTab, string> = {
  details: 'Phase Details',
  log: 'Pipeline Log',
  reasoning: 'Agent Reasoning',
}

function activeTab(value: string | null, fallback: WorkspaceTab): WorkspaceTab {
  return (TABS as readonly string[]).includes(value ?? '') ? (value as WorkspaceTab) : fallback
}

/**
 * Center workspace: [Phase Details, Pipeline Log, Agent Reasoning] tab bar
 * driven by ?tab= (deep-linkable). Default tab is Pipeline Log while the
 * thread is busy, Phase Details otherwise.
 */
export function PhaseWorkspace({
  threadId,
  phase,
  state,
  stream,
  threadBusy = false,
  gateSlot,
  appId,
  gate,
  gateState,
}: {
  threadId: string
  phase: PhaseName
  state: PipelineState
  /** Live stream view from useRunLiveness (RunDetailPage); optional for snapshot-only mounts. */
  stream?: LiveStreamViewLike
  threadBusy?: boolean
  /** D3 HITL: GateModule (gate on this phase) or slim banner, pinned above the tabs. */
  gateSlot?: ReactNode
  appId?: string | null
  gate?: GateInstance | null
  gateState?: GateMachineState
}) {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = activeTab(searchParams.get('tab'), threadBusy ? 'log' : 'details')
  const entry = state.phase_results?.[phase]

  const engineSamples = stream?.engineStats?.samples ?? []
  const liveStatus = stream?.phaseProgress?.[phase]?.status
  const showEngineStrip =
    phase === 'execution' &&
    (engineSamples.length > 0 || entry?.status === 'running' || liveStatus === 'running')
  const promptGateEditorActive =
    gate?.kind === 'prompt_review' &&
    gate.phase === phase &&
    (gateState?.tag === 'open' || gateState?.tag === 'submitting' || gateState?.tag === 'failed')

  function selectTab(next: WorkspaceTab) {
    setSearchParams(
      (params) => {
        params.set('tab', next)
        return params
      },
      { replace: true },
    )
  }

  return (
    <section className="phase-workspace glass-panel" aria-label={`${PHASE_LABELS[phase]} workspace`}>
      {gateSlot}
      {!promptGateEditorActive && (
        <PromptReviewSection
          threadId={threadId}
          phase={phase}
          state={state}
          appId={appId ?? null}
        />
      )}
      <div className="workspace-tabs" role="tablist" aria-label="Phase workspace tabs">
        {TABS.map((candidate) => (
          <button
            key={candidate}
            type="button"
            role="tab"
            className="workspace-tab"
            aria-selected={tab === candidate}
            onClick={() => selectTab(candidate)}
          >
            {TAB_LABELS[candidate]}
          </button>
        ))}
      </div>
      {showEngineStrip && (
        <EngineStrip samples={engineSamples} latest={stream?.engineStats?.latest ?? null} />
      )}
      {tab === 'details' && <PhaseDetailsTab threadId={threadId} entry={entry} state={state} />}
      {tab === 'log' && (
        <div className="pipeline-log-panel" role="tabpanel" aria-label="Pipeline log">
          <ActivityFeed
            key={`${threadId}:${phase}`}
            phase={phase}
            streamStatus={stream?.status}
            progress={stream?.phaseProgress?.[phase]}
            toolCalls={stream?.toolCalls}
            agentEvents={stream?.agentEvents}
            engineErrors={stream?.engineErrors}
            engineSamples={phase === 'execution' ? stream?.engineStats?.samples : undefined}
          />
        </div>
      )}
      {tab === 'reasoning' && <ReasoningTab phase={phase} entry={entry} state={state} />}
    </section>
  )
}

function KpiPills({ summary }: { summary: TestResultSummary }) {
  const kpis = summary.kpis ?? {}
  const pills: Array<{ label: string; value: string }> = [
    { label: 'TPS avg', value: fmtKpi(kpis['tps_avg']) },
    { label: 'p95', value: kpis['p95_ms'] !== undefined ? `${fmtKpi(kpis['p95_ms'])} ms` : '—' },
    {
      label: 'Error rate',
      value: kpis['error_rate'] !== undefined ? `${(kpis['error_rate'] * 100).toFixed(2)}%` : '—',
    },
    { label: 'VUsers peak', value: fmtKpi(kpis['vusers_peak']) },
  ]
  return (
    <div className="kpi-row" data-testid="kpi-row">
      {pills.map((pill) => (
        <span key={pill.label} className="kpi-pill">
          <span className="kpi-label">{pill.label}</span>
          <span className="kpi-value">{pill.value}</span>
        </span>
      ))}
      <span className={`status-badge ${summary.passed ? 'success' : 'danger'}`}>
        {summary.passed ? 'Passed' : 'Failed'}
      </span>
    </div>
  )
}

function fmtKpi(value: number | undefined): string {
  if (value === undefined) return '—'
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 10) / 10)
}

function ApprovalsList({ approvals }: { approvals: ApprovalRecord[] }) {
  return (
    <ul className="approvals-list">
      {approvals.map((approval) => (
        <li key={approval.id} className="approval-row">
          <span className="kind-chip">{approval.gate ?? 'gate'}</span>
          <span className="approval-action">{approval.action ?? '—'}</span>
          <span>{approval.actor ?? 'unknown actor'}</span>
          <span className="approval-at">{formatTimestamp(approval.at)}</span>
        </li>
      ))}
    </ul>
  )
}

/** D8 parity: durable per-phase tool calls from the snapshot (PhaseResult.tool_calls). */
function ToolCallsList({ calls }: { calls: ToolCallRecord[] }) {
  return (
    <ul className="approvals-list" data-testid="output-tool-calls">
      {calls.map((call) => (
        <li key={call.id} className="approval-row">
          <span className="kind-chip">tool</span>
          <span className="approval-action">{call.tool ?? call.id}</span>
          <span className={`status-badge ${call.status === 'error' ? 'danger' : 'success'}`}>
            {call.status ?? 'ok'}
          </span>
          <span>{typeof call.duration_ms === 'number' ? `${call.duration_ms} ms` : '—'}</span>
          <span className="approval-at">{call.at ? formatTimestamp(call.at) : ''}</span>
        </li>
      ))}
    </ul>
  )
}

function resolveArtifacts(
  entry: PhaseResultEntry | undefined,
  state: PipelineState,
): ArtifactRef[] {
  const ids = entry?.artifact_ids ?? []
  return ids.map(
    (id) =>
      state.artifacts?.find((artifact) => artifact.id === id) ??
      (entry?.transcript_ref?.id === id ? entry.transcript_ref : undefined) ?? { id },
  )
}

function ArtifactCards({
  threadId,
  entry,
  state,
}: {
  threadId: string
  entry: PhaseResultEntry | undefined
  state: PipelineState
}) {
  const rows = resolveArtifacts(entry, state)
  if (rows.length === 0) return null

  return (
    <>
      <h3 className="workspace-section-title">Artifacts</h3>
      <div className="artifact-card-grid">
        {rows.map((artifact) => (
          <Link key={artifact.id} className="artifact-card" to={`/runs/${threadId}/artifacts/${artifact.id}`}>
            <span className="kind-chip">{artifact.kind ?? 'artifact'}</span>
            <strong>{artifact.name ?? artifact.id}</strong>
            <span className="artifact-card-meta">{artifact.media_type ?? '—'}</span>
            <span className="artifact-card-summary">{artifact.summary ?? 'Open artifact'}</span>
          </Link>
        ))}
      </div>
    </>
  )
}

function PhaseDetailsTab({
  threadId,
  entry,
  state,
}: {
  threadId: string
  entry: PhaseResultEntry | undefined
  state: PipelineState
}) {
  if (!entry) {
    return (
      <div className="dash-empty small" role="tabpanel">
        No result for this phase yet.
        <span className="dash-empty-hint">It has not run on this thread.</span>
      </div>
    )
  }

  const paragraphs = entry.summary?.split(/\n{2,}/).filter((p) => p.trim().length > 0) ?? []

  return (
    <div role="tabpanel" aria-label="Phase details">
      {entry.test_summary && <KpiPills summary={entry.test_summary} />}
      {paragraphs.length > 0 ? (
        <div className="workspace-summary">
          {paragraphs.map((paragraph, index) => (
            <p key={index}>{paragraph}</p>
          ))}
        </div>
      ) : (
        <div className="dash-empty small">No summary recorded.</div>
      )}
      {entry.reasoning_digest && <p className="workspace-caption">{entry.reasoning_digest}</p>}
      {(entry.warnings?.length ?? 0) > 0 && (
        <>
          <h3 className="workspace-section-title">Warnings</h3>
          {entry.warnings?.map((warning, index) => (
            <div key={index} className="tonal-card warning">
              {warning}
            </div>
          ))}
        </>
      )}
      {(entry.errors?.length ?? 0) > 0 && (
        <>
          <h3 className="workspace-section-title">Errors</h3>
          {entry.errors?.map((error, index) => (
            <div key={index} className="tonal-card danger">
              {error}
            </div>
          ))}
        </>
      )}
      {(entry.approvals?.length ?? 0) > 0 && (
        <>
          <h3 className="workspace-section-title">Approvals</h3>
          <ApprovalsList approvals={entry.approvals ?? []} />
        </>
      )}
      <ArtifactCards threadId={threadId} entry={entry} state={state} />
    </div>
  )
}

function ReasoningTab({
  phase,
  entry,
  state,
}: {
  phase: PhaseName
  entry: PhaseResultEntry | undefined
  state: PipelineState
}) {
  const entries = (state.dialogue ?? []).filter((item) => item.phase === phase)
  const prompt = entry?.resolved_prompt
  const source = entry?.resolved_prompt_source
  const hasReasoning =
    Boolean(entry?.reasoning_digest) ||
    (entry?.tool_calls?.length ?? 0) > 0 ||
    entries.length > 0 ||
    Boolean(prompt?.system) ||
    Boolean(prompt?.user) ||
    Boolean(prompt?.application)

  if (!hasReasoning) {
    return (
      <div className="dash-empty small" role="tabpanel">
        No reasoning details recorded for this phase yet.
      </div>
    )
  }

  return (
    <div role="tabpanel" aria-label="Agent reasoning">
      {entry?.reasoning_digest ? (
        <>
          <h3 className="workspace-section-title">Reasoning Digest</h3>
          <p className="workspace-caption reasoning-digest">{entry.reasoning_digest}</p>
        </>
      ) : null}

      {(entry?.tool_calls?.length ?? 0) > 0 && (
        <>
          <h3 className="workspace-section-title">Tool Calls</h3>
          <ToolCallsList calls={entry?.tool_calls ?? []} />
        </>
      )}

      {(prompt?.system || prompt?.user || prompt?.application) && (
        <>
          <h3 className="workspace-section-title">Resolved Prompt</h3>
          {source?.origin && (
            <div className="kpi-row">
              <span
                className="topbar-meta-chip accent"
                data-testid="prompt-provenance"
                title="Where the winning prompt text came from"
              >
                {source.origin}
                {source.ref ? ` · ${source.ref}` : ''}
              </span>
            </div>
          )}
          {prompt?.system && <CodeViewer value={prompt.system} ariaLabel="System prompt" />}
          {prompt?.application && (
            <CodeViewer value={prompt.application} ariaLabel="Application prompt" />
          )}
          {prompt?.user && <CodeViewer value={prompt.user} ariaLabel="User prompt" />}
        </>
      )}

      {entries.length > 0 && (
        <>
          <h3 className="workspace-section-title">Operator Dialogue</h3>
          <div className="dialogue-thread">
            {entries.map((item) => (
              <div key={item.id} className={`dialogue-bubble ${item.role}`}>
                <span className="dialogue-role">
                  {item.role}
                  {item.at ? ` · ${formatTimestamp(item.at)}` : ''}
                </span>
                {item.content}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
