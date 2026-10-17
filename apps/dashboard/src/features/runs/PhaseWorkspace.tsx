import { formatPercentRate } from '@/utils/metrics'
import { useId, type ReactNode } from 'react'
import { TabList } from '@/components/ux/TabList'

import { useSearchParams } from 'react-router'

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
import { JsonViewer } from '@/components/viewers/JsonViewer'
import { ArtifactLink } from '@/features/artifacts/ArtifactLink'
import { artifactViewerPath } from '@/features/artifacts/artifactPaths'
import type { GateInstance, GateMachineState } from '@/hitl/gateMachine'

import { ActivityFeed } from './ActivityFeed'
import { EngineStrip } from './EngineStrip'
import type { LiveStreamViewLike } from './liveTypes'
import { PromptReviewSection } from './PromptReviewSection'
import { promptSourceLabel, promptSourceTone } from './promptProvenance'
import { toolCallTone, formatDuration, formatTimestamp, PHASE_LABELS, pipelineStatusLabel } from './runDisplay'

const TABS = ['details', 'log', 'reasoning'] as const
type WorkspaceTab = (typeof TABS)[number]

const TAB_LABELS: Record<WorkspaceTab, string> = {
  details: 'Phase Details',
  log: 'Pipeline Log',
  reasoning: 'Agent Reasoning',
}

function activeTab(value: string | null, fallback: WorkspaceTab): WorkspaceTab {
  if (value === 'activity') return 'log'
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
  runId,
  threadBusy = false,
  gateSlot,
  appId,
  gate,
  gateState,
  phaseStatus,
  terminal = false,
}: {
  threadId: string
  phase: PhaseName
  state: PipelineState
  /** Live stream view from useRunLiveness (RunDetailPage); optional for snapshot-only mounts. */
  stream?: LiveStreamViewLike
  runId?: string | null
  threadBusy?: boolean
  /** D3 HITL: GateModule (gate on this phase) or slim banner, pinned above the tabs. */
  gateSlot?: ReactNode
  appId?: string | null
  gate?: GateInstance | null
  gateState?: GateMachineState
  phaseStatus?: string
  terminal?: boolean
}) {
  const tabsId = useId()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = activeTab(searchParams.get('tab'), threadBusy ? 'log' : 'details')
  const durableEntry = state.phase_results?.[phase]
  const pendingEvidence = gate?.phase === phase && gate.payload?.kind === 'phase_review' ? gate.payload.evidence : undefined
  const entry = pendingEvidence ? { ...durableEntry, ...pendingEvidence } : durableEntry

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
      <PhaseOperatorSummary entry={entry} status={phaseStatus} />
      <TabList id={tabsId} label="Phase workspace tabs" items={TABS.map(id => ({id, label: TAB_LABELS[id]}))} value={tab} onChange={selectTab} />
      <div id={`${tabsId}-panel-${tab}`} role="tabpanel" aria-labelledby={`${tabsId}-tab-${tab}`} tabIndex={0}>
      {gate?.phase === phase && tab === 'details' && <p className="ux-context">The pending output is shown in the review above. Available agent evidence appears below before you decide.</p>}
      {showEngineStrip && (
        <EngineStrip samples={engineSamples} latest={stream?.engineStats?.latest ?? null} />
      )}
      {tab === 'details' && <PhaseDetailsTab threadId={threadId} entry={entry} state={state} />}
      {tab === 'log' && (
        <div className="pipeline-log-panel" aria-label="Pipeline log">
          <ActivityFeed
            key={JSON.stringify([threadId, phase, runId ?? 'snapshot'])}
            phase={phase}
            terminal={terminal}
            streamStatus={stream?.status}
            progress={stream?.phaseProgress?.[phase]}
            toolCalls={stream?.toolCalls}
            agentEvents={stream?.agentEvents}
            engineErrors={stream?.engineErrors}
            engineSamples={phase === 'execution' ? stream?.engineStats?.samples : undefined}
          />
        </div>
      )}
      {tab === 'reasoning' && <ReasoningTab threadId={threadId} phase={phase} entry={entry} state={state} />}
      </div>
      {!promptGateEditorActive && (
        <details className="prompt-configuration">
          <summary>Prompt configuration</summary>
        <PromptReviewSection
          key={JSON.stringify([threadId, phase])}
          threadId={threadId}
          phase={phase}
          state={state}
          appId={appId ?? null}
        />
        </details>
      )}

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
      value: formatPercentRate(kpis['error_rate']),
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
      <span className={`status-badge ${summary.passed === true ? 'success' : summary.passed === false ? 'danger' : 'neutral'}`}>
        {summary.passed === true ? 'Passed' : summary.passed === false ? 'Failed' : 'Not available'}
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
export function ToolCallsList({ calls }: { calls: ToolCallRecord[] }) {
  return (
    <ul className="approvals-list" data-testid="output-tool-calls">
      {calls.map((call) => (
        <li key={call.id} className="approval-row">
          <span className="kind-chip">tool</span>
          <span className="approval-action">{call.tool ?? call.id}</span>
          <span className={`status-badge ${toolCallTone(call.status)}`}>
            {call.status ?? 'Not recorded'}
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
          <ArtifactLink
            key={artifact.id}
            className="artifact-card"
            to={artifactViewerPath(threadId, artifact.id)}
          >
            <span className="kind-chip">{artifact.kind ?? 'artifact'}</span>
            <strong>{artifact.name ?? artifact.id}</strong>
            <span className="artifact-card-meta">{artifact.media_type ?? '—'}</span>
            <span className="artifact-card-summary">{artifact.summary ?? 'Open artifact'}</span>
          </ArtifactLink>
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
      <div className="dash-empty small">
        No result recorded for this phase.
        <span className="dash-empty-hint">The snapshot contains no phase result.</span>
      </div>
    )
  }

  return (
    <div aria-label="Phase details">
      <p className="workspace-caption">
        Output source: {entry.provenance ? ({ stub: 'Stub — demonstration output', llm: 'AI-generated', simulator: 'Simulator — synthetic execution', live_provider: 'Live execution provider', imported: 'Imported evidence' }[entry.provenance]) : 'Not recorded'}
      </p>
      {entry.test_summary && <KpiPills summary={entry.test_summary} />}

      {(entry.approvals?.length ?? 0) > 0 && (
        <>
          <h3 className="workspace-section-title">Approvals</h3>
          <ApprovalsList approvals={entry.approvals ?? []} />
        </>
      )}
      <ArtifactCards threadId={threadId} entry={entry} state={state} />
      <details className="phase-technical-output">
        <summary>Technical phase output</summary>
        <JsonViewer value={entry} ariaLabel="Phase output JSON" />
      </details>
    </div>
  )
}

function PhaseOperatorSummary({ entry, status }: { entry: PhaseResultEntry | undefined; status?: string }) {
  if (!entry) {
    return (
      <section className="phase-operator-summary" aria-label="Phase snapshot">
        <h3>What happened</h3>
        <p>No result recorded for this phase.</p>
      </section>
    )
  }

  const paragraphs = entry.summary?.split(/\n{2,}/).filter((p) => p.trim().length > 0) ?? []
  const artifactCount = entry.artifact_ids?.length ?? 0
  return (
    <section className="phase-operator-summary" aria-label="Phase snapshot">
      <dl className="phase-operator-metrics">
        <div><dt>Status</dt><dd>{pipelineStatusLabel(status ?? entry.status)}</dd></div>
        <div><dt>Attempt</dt><dd>{entry.attempt ?? 'Not started'}</dd></div>
        <div><dt>Elapsed time (includes reviews)</dt><dd>{formatDuration(entry.duration_s)}</dd></div>
        <div><dt>Started</dt><dd>{formatTimestamp(entry.started_at)}</dd></div>
        <div><dt>Evidence</dt><dd>{artifactCount === 0 ? 'No phase artifacts' : `${artifactCount} phase artifact${artifactCount === 1 ? '' : 's'}`}</dd></div>
      </dl>
      {(entry.warnings?.length ?? 0) > 0 && (
        <div className="phase-operator-attention">
          <h3 className="workspace-section-title">Needs attention</h3>
          {entry.warnings?.map((warning, index) => <div key={index} className="tonal-card warning">{warning}</div>)}
        </div>
      )}
      {(entry.errors?.length ?? 0) > 0 && (
        <div className="phase-operator-attention">
          <h3 className="workspace-section-title">Errors</h3>
          {entry.errors?.map((error, index) => <div key={index} className="tonal-card danger">{error}</div>)}
        </div>
      )}
      <h3>What happened</h3>
      <p className="workspace-caption">Output source: {entry.provenance === 'llm' ? 'AI-generated' : entry.provenance === 'stub' ? 'Stub — demonstration output' : entry.provenance?.replaceAll('_', ' ') ?? 'Not recorded'}</p>
      {paragraphs.length > 0 ? paragraphs.map((paragraph, index) => <p key={index}>{paragraph}</p>) : <p>No operator-readable summary has been recorded for this phase.</p>}
    </section>
  )
}

export function ReasoningTab({
  threadId,
  phase,
  entry,
  state,
}: {
  threadId: string
  phase: PhaseName
  entry: PhaseResultEntry | undefined
  state: PipelineState
}) {
  const entries = (state.dialogue ?? []).filter((item) => item.phase === phase)
  const prompt = entry?.resolved_prompt
  const source = entry?.resolved_prompt_source
  const hasReasoning =
    Boolean(entry?.transcript_ref) ||
    Boolean(entry?.reasoning_digest) ||
    (entry?.tool_calls?.length ?? 0) > 0 ||
    entries.length > 0 ||
    Boolean(prompt?.system) ||
    Boolean(prompt?.user) ||
    Boolean(prompt?.application)

  if (!hasReasoning) {
    return (
      <div className="dash-empty small">
        No reasoning details recorded for this phase.
      </div>
    )
  }

  return (
    <div aria-label="Agent reasoning">
      {entry?.transcript_ref && <p><ArtifactLink to={artifactViewerPath(threadId, entry.transcript_ref.id)}>Full agent transcript{entry.attempt ? ` (attempt ${entry.attempt})` : ''}</ArtifactLink> — the durable record of this phase’s reasoning, prompts and tool calls.</p>}
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
                className={`topbar-meta-chip prompt-provenance-chip ${promptSourceTone(source)}`}
                data-testid="prompt-provenance"
                title={promptSourceLabel(source)}
              >
                {promptSourceLabel(source)}
              </span>
            </div>
          )}
          {prompt?.system && <><h4>System prompt</h4><CodeViewer value={prompt.system} ariaLabel="System prompt" /></>}
          {prompt?.application && (
            <><h4>Application prompt</h4><CodeViewer value={prompt.application} ariaLabel="Application prompt" /></>
          )}
          {prompt?.user && <><h4>User prompt</h4><CodeViewer value={prompt.user} ariaLabel="User prompt" /></>}
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
