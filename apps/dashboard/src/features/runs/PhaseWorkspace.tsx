import { Link, useSearchParams } from 'react-router'

import type {
  ApprovalRecord,
  ArtifactRef,
  PhaseName,
  PhaseResultEntry,
  PipelineState,
  TestResultSummary,
} from '@apex/pipeline-events'

import { CodeViewer } from '@/components/viewers/CodeViewer'

import { formatTimestamp, PHASE_LABELS } from './runDisplay'

const TABS = ['output', 'artifacts', 'prompt', 'dialogue'] as const
type WorkspaceTab = (typeof TABS)[number]

const TAB_LABELS: Record<WorkspaceTab, string> = {
  output: 'Output',
  artifacts: 'Artifacts',
  prompt: 'Prompt',
  dialogue: 'Dialogue',
}

function activeTab(value: string | null): WorkspaceTab {
  return (TABS as readonly string[]).includes(value ?? '') ? (value as WorkspaceTab) : 'output'
}

/**
 * Center workspace: [Output, Artifacts, Prompt, Dialogue] tab bar driven by
 * ?tab= (deep-linkable; default output).
 */
export function PhaseWorkspace({
  threadId,
  phase,
  state,
}: {
  threadId: string
  phase: PhaseName
  state: PipelineState
}) {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = activeTab(searchParams.get('tab'))
  const entry = state.phase_results?.[phase]

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
