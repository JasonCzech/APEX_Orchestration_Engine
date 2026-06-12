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

import { ActivityFeed } from './ActivityFeed'
import { EngineStrip } from './EngineStrip'
import type { LiveStreamViewLike } from './liveTypes'
import { formatTimestamp, PHASE_LABELS } from './runDisplay'

const TABS = ['activity', 'output', 'artifacts', 'prompt', 'dialogue'] as const
type WorkspaceTab = (typeof TABS)[number]

const TAB_LABELS: Record<WorkspaceTab, string> = {
  activity: 'Activity',
  output: 'Output',
  artifacts: 'Artifacts',
  prompt: 'Prompt',
  dialogue: 'Dialogue',
}

function activeTab(value: string | null, fallback: WorkspaceTab): WorkspaceTab {
  return (TABS as readonly string[]).includes(value ?? '') ? (value as WorkspaceTab) : fallback
}

/**
 * Center workspace: [Activity, Output, Artifacts, Prompt, Dialogue] tab bar
 * driven by ?tab= (deep-linkable). Default tab is Activity while the thread is
 * busy (live deltas front and center), Output otherwise (D2).
 */
export function PhaseWorkspace({
  threadId,
  phase,
  state,
  stream,
  threadBusy = false,
  gateSlot,
}: {
  threadId: string
  phase: PhaseName
  state: PipelineState
  /** Live stream view from useRunLiveness (RunDetailPage); optional for snapshot-only mounts. */
  stream?: LiveStreamViewLike
  threadBusy?: boolean
  /** D3 HITL: GateModule (gate on this phase) or slim banner, pinned above the tabs. */
  gateSlot?: ReactNode
}) {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = activeTab(searchParams.get('tab'), threadBusy ? 'activity' : 'output')
  const entry = state.phase_results?.[phase]

  // Engine strip: execution phase only, when the stream has poll samples or
  // the phase is currently running (snapshot or live status).
  const engineSamples = stream?.engineStats?.samples ?? []
  const liveStatus = stream?.phaseProgress?.[phase]?.status
  const showEngineStrip =
    phase === 'execution' &&
    (engineSamples.length > 0 || entry?.status === 'running' || liveStatus === 'running')

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
      {tab === 'activity' && (
        <ActivityFeed
          key={phase}
          phase={phase}
          streamStatus={stream?.status}
          progress={stream?.phaseProgress?.[phase]}
          toolCalls={stream?.toolCalls}
          engineSamples={phase === 'execution' ? stream?.engineStats?.samples : undefined}
        />
      )}
      {tab === 'output' && <OutputTab entry={entry} />}
      {tab === 'artifacts' && <ArtifactsTab threadId={threadId} entry={entry} state={state} />}
      {tab === 'prompt' && <PromptTab entry={entry} />}
      {tab === 'dialogue' && <DialogueTab phase={phase} state={state} />}
    </section>
  )
}

/* ── Output ──────────────────────────────────────────────────────────────── */

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

/** D8 parity: durable per-phase tool calls from the snapshot (PhaseResult.tool_calls) —
 *  the Activity tab shows only the live stream's tool_call events. */
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

function OutputTab({ entry }: { entry: PhaseResultEntry | undefined }) {
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
    <div role="tabpanel" aria-label="Output">
      {entry.test_summary && <KpiPills summary={entry.test_summary} />}
      {/* Plain-text paragraphs for now — no markdown renderer dependency in D1. */}
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
      {(entry.tool_calls?.length ?? 0) > 0 && (
        <>
          <h3 className="workspace-section-title">Tool calls</h3>
          <ToolCallsList calls={entry.tool_calls ?? []} />
        </>
      )}
      {(entry.approvals?.length ?? 0) > 0 && (
        <>
          <h3 className="workspace-section-title">Approvals</h3>
          <ApprovalsList approvals={entry.approvals ?? []} />
        </>
      )}
    </div>
  )
}

/* ── Artifacts ───────────────────────────────────────────────────────────── */

function ArtifactsTab({
  threadId,
  entry,
  state,
}: {
  threadId: string
  entry: PhaseResultEntry | undefined
  state: PipelineState
}) {
  const ids = entry?.artifact_ids ?? []
  if (ids.length === 0) {
    return (
      <div className="dash-empty small" role="tabpanel">
        No artifacts for this phase.
      </div>
    )
  }
  // Resolve ids against the run-level artifacts index; fall back to the
  // phase's transcript_ref, then to an id-only row so nothing disappears.
  const rows: ArtifactRef[] = ids.map(
    (id) =>
      state.artifacts?.find((artifact) => artifact.id === id) ??
      (entry?.transcript_ref?.id === id ? entry?.transcript_ref : undefined) ?? { id },
  )
  return (
    <div className="data-table-wrap" role="tabpanel" aria-label="Artifacts">
      <table className="data-table">
        <thead>
          <tr>
            <th>Kind</th>
            <th>Name</th>
            <th>Media type</th>
            <th>Summary</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((artifact) => (
            <tr key={artifact.id}>
              <td>
                <span className="kind-chip">{artifact.kind ?? 'artifact'}</span>
              </td>
              <td>
                {/* Route param :name carries the ARTIFACT ID, not the filename. */}
                <Link className="artifact-link" to={`/runs/${threadId}/artifacts/${artifact.id}`}>
                  {artifact.name ?? artifact.id}
                </Link>
              </td>
              <td className="mono">{artifact.media_type ?? '—'}</td>
              <td>{artifact.summary ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ── Prompt ──────────────────────────────────────────────────────────────── */

function PromptTab({ entry }: { entry: PhaseResultEntry | undefined }) {
  const prompt = entry?.resolved_prompt
  if (!prompt || (!prompt.system && !prompt.user)) {
    return (
      <div className="dash-empty small" role="tabpanel">
        No resolved prompt recorded for this phase.
      </div>
    )
  }
  const source = entry?.resolved_prompt_source
  return (
    <div role="tabpanel" aria-label="Prompt">
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
      {prompt.system && (
        <>
          <h3 className="workspace-section-title">System</h3>
          <CodeViewer value={prompt.system} ariaLabel="System prompt" />
        </>
      )}
      {prompt.user && (
        <>
          <h3 className="workspace-section-title">User</h3>
          <CodeViewer value={prompt.user} ariaLabel="User prompt" />
        </>
      )}
    </div>
  )
}

/* ── Dialogue ────────────────────────────────────────────────────────────── */

function DialogueTab({ phase, state }: { phase: PhaseName; state: PipelineState }) {
  const entries = (state.dialogue ?? []).filter((item) => item.phase === phase)
  if (entries.length === 0) {
    return (
      <div className="dash-empty small" role="tabpanel">
        No dialogue for this phase
      </div>
    )
  }
  return (
    <div className="dialogue-thread" role="tabpanel" aria-label="Dialogue">
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
  )
}
