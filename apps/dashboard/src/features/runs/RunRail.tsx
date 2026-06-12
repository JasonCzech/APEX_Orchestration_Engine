import { useMemo, useState } from 'react'
import { Link } from 'react-router'

import {
  PHASE_NAMES,
  type ApprovalRecord,
  type ArtifactRef,
  type PhaseName,
  type PipelineState,
} from '@apex/pipeline-events'

import type { GateInterrupt, PipelineDetail } from '@/api/hooks/useThreadState'

import { normalizeGateHint, type LiveGateHint } from './liveTypes'
import { formatTimestamp, PHASE_LABELS, isPhaseName } from './runDisplay'

interface ArtifactGroup {
  label: string
  artifacts: ArtifactRef[]
}

/** Group the run-level artifact index by owning phase via phase_results.artifact_ids. */
function groupArtifacts(state: PipelineState): ArtifactGroup[] {
  const artifacts = state.artifacts ?? []
  const owner = new Map<string, PhaseName>()
  for (const phase of PHASE_NAMES) {
    for (const id of state.phase_results?.[phase]?.artifact_ids ?? []) {
      if (!owner.has(id)) owner.set(id, phase)
    }
  }
  const groups: ArtifactGroup[] = []
  for (const phase of PHASE_NAMES) {
    const owned = artifacts.filter((artifact) => owner.get(artifact.id) === phase)
    if (owned.length > 0) groups.push({ label: PHASE_LABELS[phase], artifacts: owned })
  }
  const orphaned = artifacts.filter((artifact) => !owner.has(artifact.id))
  if (orphaned.length > 0) groups.push({ label: 'Other', artifacts: orphaned })
  return groups
}

interface ApprovalRow extends ApprovalRecord {
  phase: string
}

/** Flatten approvals across phases, newest first. */
function approvalHistory(state: PipelineState): ApprovalRow[] {
  const rows: ApprovalRow[] = []
  for (const phase of PHASE_NAMES) {
    for (const approval of state.phase_results?.[phase]?.approvals ?? []) {
      rows.push({ ...approval, phase })
    }
  }
  return rows.sort((a, b) => (b.at ?? '').localeCompare(a.at ?? ''))
}

/**
 * Right rail (320px, collapsible): run metadata, all-artifacts index grouped by
 * phase, compact approvals history, and a pending-gate banner placeholder
 * (gate actions land in D3 — the action link is disabled with a tooltip).
 */
export function RunRail({
  detail,
  state,
  interrupts,
  pendingGateHint,
}: {
  detail: PipelineDetail
  state: PipelineState
  interrupts: GateInterrupt[]
  /** gate_opened stream accelerator — shown until the snapshot poll delivers the interrupt (D2). */
  pendingGateHint?: LiveGateHint | string | null
}) {
  const [collapsed, setCollapsed] = useState(false)
  const groups = useMemo(() => groupArtifacts(state), [state])
  const approvals = useMemo(() => approvalHistory(state), [state])
  const gate = interrupts[0]
  // The hydrated interrupt always wins; the hint only bridges the poll gap.
  const hint = gate ? null : normalizeGateHint(pendingGateHint)

  if (collapsed) {
    return (
      <aside className="run-rail glass-panel collapsed" aria-label="Run details rail">
        <button
          type="button"
          className="run-rail-toggle"
          aria-label="Expand run rail"
          onClick={() => setCollapsed(false)}
        >
          ◀
        </button>
      </aside>
    )
  }

  return (
    <aside className="run-rail glass-panel" aria-label="Run details rail">
      <button
        type="button"
        className="run-rail-toggle"
        aria-label="Collapse run rail"
        onClick={() => setCollapsed(true)}
      >
        ▶
      </button>

      {gate && (
        <div className="gate-banner" role="status">
          <span>
            Gate open: <strong>{gate.kind ?? 'review'}</strong> on{' '}
            <strong>{isPhaseName(gate.phase) ? PHASE_LABELS[gate.phase] : (gate.phase ?? '?')}</strong>
          </span>
          {/* D3: links to the gate's phase, where the GateModule is pinned
              above the workspace tabs. */}
          <Link
            className="btn btn-secondary btn-sm gate-banner-action"
            to={
              isPhaseName(gate.phase)
                ? `/runs/${detail.thread_id}/phases/${gate.phase}`
                : `/runs/${detail.thread_id}`
            }
          >
            Review gate
          </Link>
        </div>
      )}

      {hint && (
        <div className="gate-banner" role="status" data-testid="gate-hint">
          <span>
            Gate opening: <strong>{hint.gate ?? 'review'}</strong>
            {isPhaseName(hint.phase) ? (
              <>
                {' '}
                on <strong>{PHASE_LABELS[hint.phase]}</strong>
              </>
            ) : hint.phase ? (
              <>
                {' '}
                on <strong>{hint.phase}</strong>
              </>
            ) : null}
          </span>
          <span
            className="topbar-meta-chip warning gate-hint-chip"
            title="Heard on the live stream — fetching the full gate payload; the review opens here as soon as it lands"
          >
            loading gate…
          </span>
        </div>
      )}

      <section>
        <h3 className="run-rail-section-title">Run</h3>
        <dl className="run-meta">
          <dt>Thread</dt>
          <dd>
            <span className="mono" title={detail.thread_id}>
              {detail.thread_id}
            </span>
            <button
              type="button"
              className="copy-button"
              onClick={() => void navigator.clipboard?.writeText(detail.thread_id)}
            >
              Copy
            </button>
          </dd>
          <dt>Project</dt>
          <dd>{detail.project_id ?? '—'}</dd>
          <dt>App</dt>
          <dd>{detail.app_id ?? '—'}</dd>
          <dt>Engine</dt>
          <dd>
            {state.engine_handle?.engine ? (
              <span className="topbar-meta-chip accent">
                {state.engine_handle.engine}
                {state.engine_handle.external_run_id
                  ? ` · ${state.engine_handle.external_run_id}`
                  : ''}
              </span>
            ) : (
              '—'
            )}
          </dd>
          <dt>Created</dt>
          <dd>{formatTimestamp(detail.created_at)}</dd>
          <dt>Updated</dt>
          <dd>{formatTimestamp(detail.updated_at)}</dd>
        </dl>
      </section>

      <section aria-label="Artifacts index">
        <h3 className="run-rail-section-title">Artifacts</h3>
        {groups.length === 0 ? (
          <div className="dash-empty small">No artifacts yet.</div>
        ) : (
          groups.map((group) => (
            <div key={group.label} className="rail-artifact-group">
              <h4 className="rail-artifact-group-name">{group.label}</h4>
              <ul className="rail-artifact-list">
                {group.artifacts.map((artifact) => (
                  <li key={artifact.id}>
                    <Link to={`/runs/${detail.thread_id}/artifacts/${artifact.id}`}>
                      <span className="kind-chip">{artifact.kind ?? 'artifact'}</span>
                      <span className="rail-artifact-name">{artifact.name ?? artifact.id}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </section>

      <section aria-label="Approvals history">
        <h3 className="run-rail-section-title">Approvals</h3>
        {approvals.length === 0 ? (
          <div className="dash-empty small">No approvals yet.</div>
        ) : (
          <ul className="rail-approvals">
            {approvals.map((approval) => (
              <li key={approval.id}>
                <span className="approval-phase">
                  {isPhaseName(approval.phase) ? PHASE_LABELS[approval.phase] : approval.phase}
                </span>
                <span>
                  {approval.gate ?? 'gate'} · <strong>{approval.action ?? '—'}</strong> by{' '}
                  {approval.actor ?? 'unknown'}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  )
}
