/**
 * / — Home dashboard (plan UX 1.5): attention-first overview.
 *
 * Layout: main column (needs-attention rail → active-runs grid → recent
 * table) + sticky 320px right panel (Release-Snapshot pattern: New Run CTA,
 * 7-day usage snapshot, resumable drafts, system-health footer line).
 *
 * Data: ONE unfiltered usePipelines poll (HOME_FLEET_FILTER) sliced three
 * ways in homeLogic, plus useApprovalsInbox for the gates rail — that hook
 * shares the sidebar badge's cache entry, so it costs no extra request.
 *
 * NOTE (plan F1): live sparklines are deliberately NOT on these cards —
 * fleet liveness is poll-based (15s pipelines list); per-run SSE streams
 * attach only on the run detail page.
 */
import { Link, useNavigate } from 'react-router'

import { useUsageAnalytics } from '@/api/hooks/useAnalytics'
import { useDraftsList, type DraftRead } from '@/api/hooks/useDrafts'
import { usePipelines, type PipelineSummary } from '@/api/hooks/usePipelines'
import { isApiError } from '@/api/errors'
import { ProblemCard } from '@/components/ProblemCard'
import { PhaseStrip } from '@/components/runs/PhaseStrip'
import { gateKindTone } from '@/features/approvals/ApprovalsQueue'
import { useApprovalsInbox, type ApprovalItem } from '@/features/approvals/useApprovalsInbox'
import { useConnectivity } from '@/health/ConnectivityProvider'
import type { ConnectivityStatus } from '@/health/useSystemHealth'
import { formatRelative } from '@/utils/time'

import {
  ATTENTION_GATES_LIMIT,
  HOME_FLEET_FILTER,
  PANEL_DRAFTS_LIMIT,
  activeRuns,
  errorRateOf,
  failedRuns,
  gateHref,
  recentRuns,
  statusTone,
} from './homeLogic'
import './home.css'

const HEALTH_LABEL: Record<ConnectivityStatus, string> = {
  ok: 'Connected',
  unknown: 'Checking…',
  degraded: 'Degraded',
  unreachable: 'Unreachable',
}

const EM_DASH = '—'

function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return 'The dashboard could not be loaded.'
}

/* ── Needs-attention rail ──────────────────────────────────────────── */

function AttentionRail({
  gates,
  failures,
}: {
  gates: ApprovalItem[]
  failures: PipelineSummary[]
}) {
  return (
    <section
      className="glass-panel home-attention"
      aria-label="Needs attention"
      data-testid="home-attention"
    >
      <h2 className="home-section-title">Needs attention</h2>
      <ul className="home-attention-list">
        {gates.map((gate) => (
          <li key={gate.thread_id}>
            <Link
              to={gateHref(gate.thread_id, gate.pending_gate.interrupt_id)}
              className="home-attention-row warning"
              data-testid={`home-gate-${gate.thread_id}`}
            >
              <span className={`topbar-meta-chip ${gateKindTone(gate.pending_gate.kind)}`}>
                {gate.pending_gate.kind ?? 'review'}
              </span>
              <span className="home-attention-title">{gate.title}</span>
              <span className="home-attention-phase">{gate.pending_gate.phase ?? EM_DASH}</span>
              <span className="home-attention-age">{gate.age}</span>
            </Link>
          </li>
        ))}
        {failures.map((run) => (
          <li key={run.thread_id}>
            <Link
              to={`/runs/${run.thread_id}`}
              className="home-attention-row danger"
              data-testid={`home-failure-${run.thread_id}`}
            >
              <span className="topbar-meta-chip danger">failed</span>
              <span className="home-attention-title">{run.title || 'Untitled run'}</span>
              <span className="home-attention-age" title={run.updated_at ?? undefined}>
                {formatRelative(run.updated_at)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}

/* ── Active runs grid ──────────────────────────────────────────────── */

function ActiveRunCard({ run }: { run: PipelineSummary }) {
  const gated = run.thread_status === 'interrupted'
  return (
    <Link
      to={`/runs/${run.thread_id}`}
      className="glass-panel home-active-card"
      data-testid={`home-active-${run.thread_id}`}
    >
      <header className="home-active-head">
        <span className="home-active-title">{run.title || 'Untitled run'}</span>
        <span className={`status-badge ${statusTone(run.thread_status)}`}>
          {run.thread_status ?? 'unknown'}
        </span>
      </header>
      <PhaseStrip strip={run.phase_strip} size="sm" />
      <footer className="home-active-foot">
        <span className={`home-active-phase ${gated ? 'gated' : 'busy'}`}>
          <span className="status-dot" aria-hidden="true" />
          {run.current_phase ?? EM_DASH}
        </span>
        {run.pending_gate && (
          <span className="topbar-meta-chip warning">
            gate: {run.pending_gate.kind ?? 'review'}
          </span>
        )}
      </footer>
    </Link>
  )
}

/* ── Recent runs table ─────────────────────────────────────────────── */

function RecentRunRow({ run }: { run: PipelineSummary }) {
  const navigate = useNavigate()
  const runPath = `/runs/${run.thread_id}`
  return (
    <tr
      className="home-recent-row"
      onClick={() => navigate(runPath)}
      data-testid={`home-recent-${run.thread_id}`}
    >
      <td>
        <Link to={runPath} className="home-recent-link" onClick={(event) => event.stopPropagation()}>
          <span className="strong">{run.title || 'Untitled run'}</span>
        </Link>
      </td>
      <td>
        <span className={`status-badge ${statusTone(run.thread_status)}`}>
          {run.thread_status ?? 'unknown'}
        </span>
      </td>
      <td>
        <PhaseStrip strip={run.phase_strip} size="sm" />
      </td>
      <td className="home-recent-time" title={run.updated_at ?? undefined}>
        {formatRelative(run.updated_at)}
      </td>
    </tr>
  )
}

/* ── Right panel sections ──────────────────────────────────────────── */

function UsageSnapshot() {
  // Server defaults the window to "now minus 7 days" when from/to are omitted.
  const usage = useUsageAnalytics({})
  if (usage.isError) return null // degrade quietly — usage is a nicety here

  const totals = usage.data?.totals
  const runs = usage.data?.runs
  const value = (n: number | undefined) => (n === undefined ? EM_DASH : n.toLocaleString())

  return (
    <section className="glass-panel home-usage" data-testid="home-usage">
      <h2 className="home-section-title">Usage · last 7 days</h2>
      <dl className="home-usage-stats">
        <div className="home-usage-stat">
          <dt>Phases OK</dt>
          <dd data-testid="home-usage-succeeded">{value(runs?.phases_succeeded)}</dd>
        </div>
        <div className="home-usage-stat">
          <dt>Phases failed</dt>
          <dd
            data-testid="home-usage-failed"
            className={runs && runs.phases_failed > 0 ? 'danger' : ''}
          >
            {value(runs?.phases_failed)}
          </dd>
        </div>
        <div className="home-usage-stat">
          <dt>Events</dt>
          <dd data-testid="home-usage-events">{value(totals?.events)}</dd>
        </div>
        <div className="home-usage-stat">
          <dt>Error rate</dt>
          <dd data-testid="home-usage-error-rate">{errorRateOf(totals)}</dd>
        </div>
      </dl>
    </section>
  )
}

function DraftsPanel({ drafts }: { drafts: DraftRead[] }) {
  if (drafts.length === 0) return null
  const items = [...drafts]
    .sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? ''))
    .slice(0, PANEL_DRAFTS_LIMIT)
  return (
    <section className="glass-panel home-drafts" data-testid="home-drafts">
      <h2 className="home-section-title">Resume a draft</h2>
      <ul className="home-drafts-list">
        {items.map((draft) => (
          <li key={draft.id}>
            <Link
              to={`/runs/new?draft=${draft.id}`}
              className="home-draft-row"
              data-testid={`home-draft-${draft.id}`}
            >
              <span className="home-draft-title">{draft.title || 'Untitled draft'}</span>
              <span className="home-draft-updated" title={draft.updated_at ?? undefined}>
                {formatRelative(draft.updated_at)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}

/* ── Page ──────────────────────────────────────────────────────────── */

function HomeSkeleton() {
  return (
    <div
      className="home-skeleton"
      role="status"
      aria-busy="true"
      aria-label="Loading dashboard"
    >
      <div className="glass-panel home-skeleton-block" />
      <div className="glass-panel home-skeleton-block" />
      <div className="glass-panel home-skeleton-block tall" />
    </div>
  )
}

export function HomePage() {
  const fleet = usePipelines(HOME_FLEET_FILTER)
  const inbox = useApprovalsInbox()
  const drafts = useDraftsList()
  const { status: health } = useConnectivity()

  const items = fleet.data?.items ?? []
  const gates = inbox.items.slice(0, ATTENTION_GATES_LIMIT)
  const failures = failedRuns(items)
  const active = activeRuns(items)
  const recent = recentRuns(items)
  const draftItems = drafts.data ?? []

  if (fleet.isPending) {
    return (
      <section className="home-page animate-enter">
        <HomeSkeleton />
      </section>
    )
  }
  if (fleet.isError && !fleet.data) {
    return (
      <section className="home-page animate-enter">
        <ProblemCard
          title="Dashboard unavailable"
          message={errorMessage(fleet.error)}
          onRetry={() => void fleet.refetch()}
        />
      </section>
    )
  }

  // Empty everything (no runs, no drafts) → welcoming first-launch hero.
  if (items.length === 0 && !drafts.isPending && draftItems.length === 0) {
    return (
      <section className="home-page animate-enter">
        <div className="dash-empty home-hero" data-testid="home-hero">
          <h2>Start your first pipeline</h2>
          <p className="dash-empty-hint">
            Launch a run to see live phase progress, pending gates and usage here.
          </p>
          <Link to="/runs/new" className="btn btn-primary">
            New Run
          </Link>
        </div>
      </section>
    )
  }

  return (
    <section className="home-page animate-enter">
      <div className="home-layout">
        <div className="home-main">
          {/* Rail collapses to nothing when there is nothing to flag. */}
          {(gates.length > 0 || failures.length > 0) && (
            <AttentionRail gates={gates} failures={failures} />
          )}

          {active.length > 0 && (
            <section className="home-active" aria-label="Active runs">
              <h2 className="home-section-title">Active runs</h2>
              <div className="home-active-grid" data-testid="home-active-grid">
                {active.map((run) => (
                  <ActiveRunCard key={run.thread_id} run={run} />
                ))}
              </div>
            </section>
          )}

          <section className="home-recent" aria-label="Recent runs" data-testid="home-recent">
            <header className="home-section-head">
              <h2 className="home-section-title">Recent runs</h2>
              <Link to="/runs" className="home-view-all">
                View all
              </Link>
            </header>
            {recent.length === 0 ? (
              <div className="dash-empty compact">
                <p className="dash-empty-hint">No runs yet — resume a draft or start a new run.</p>
              </div>
            ) : (
              <div className="data-table-wrap">
                <table className="data-table striped home-recent-table">
                  <thead>
                    <tr>
                      <th>Run</th>
                      <th>Status</th>
                      <th>Phases</th>
                      <th>Updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recent.map((run) => (
                      <RecentRunRow key={run.thread_id} run={run} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>

        <aside className="home-panel" data-testid="home-panel">
          <Link to="/runs/new" className="btn btn-primary home-new-run">
            New Run
          </Link>
          <UsageSnapshot />
          <DraftsPanel drafts={draftItems} />
          <footer className={`home-health ${health}`} data-testid="home-health">
            <span className="status-dot" aria-hidden="true" />
            <span className="status-text">API: {HEALTH_LABEL[health]}</span>
          </footer>
        </aside>
      </div>
    </section>
  )
}
