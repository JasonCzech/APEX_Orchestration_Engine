import { useMemo } from 'react'
import { Link } from 'react-router'

import { useUsageAnalytics } from '@/api/hooks/useAnalytics'
import { useDraftsList } from '@/api/hooks/useDrafts'
import { usePipelines, type PipelineSummary } from '@/api/hooks/usePipelines'
import { isApiError } from '@/api/errors'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { useTopbarContribution } from '@/components/layout/TopbarContributionProvider'
import { ProblemCard } from '@/components/ProblemCard'
import { useApprovalsInbox, type ApprovalItem } from '@/features/approvals/useApprovalsInbox'
import { formatRelative } from '@/utils/time'

import {
  ATTENTION_GATES_LIMIT,
  HOME_FLEET_FILTER,
  HOME_FLEET_LIMIT,
  activeRuns,
  avgDurationLabel,
  failedRuns,
  gateHref,
  goVerdicts,
  interruptedRuns,
  recentRuns,
  statusTone,
  totalRuns,
} from './homeLogic'
import './home.css'

function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return 'The dashboard could not be loaded.'
}

function QueryErrorNotice({
  title,
  error,
  onRetry,
}: {
  title: string
  error: unknown
  onRetry: () => void
}) {
  return (
    <div className="tonal-card danger" role="alert">
      <strong>{title}</strong>: {errorMessage(error)}{' '}
      <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}

function MetricCard({
  label,
  value,
  tone,
  hint,
}: {
  label: string
  value: string | number
  tone?: 'success' | 'warning' | 'danger' | 'accent' | 'results'
  hint?: string
}) {
  return (
    <article className={`glass-panel home-metric-card${tone ? ` ${tone}` : ''}`}>
      <span className="home-metric-label">{label}</span>
      <strong className="home-metric-value">{value}</strong>
      {hint ? <span className="home-metric-hint">{hint}</span> : null}
    </article>
  )
}

function ApprovalQueue({ gates }: { gates: ApprovalItem[] }) {
  if (gates.length === 0) {
    return (
      <div className="dash-empty small">
        No pending approvals right now.
      </div>
    )
  }

  return (
    <ul className="home-approval-list" data-testid="home-approvals-list">
      {gates.map((gate) => (
        <li key={gate.thread_id}>
          <Link to={gateHref(gate.thread_id, gate.pending_gate.interrupt_id)} className="home-approval-row">
            <span className="topbar-meta-chip warning">{gate.pending_gate.kind ?? 'review'}</span>
            <span className="home-approval-title">{gate.title}</span>
            <span className="home-approval-phase">{gate.pending_gate.phase ?? 'phase'}</span>
            <span className="home-approval-age">{gate.age}</span>
          </Link>
        </li>
      ))}
    </ul>
  )
}

function RecentRunRow({ run }: { run: PipelineSummary }) {
  const runPath = `/runs/${run.thread_id}`

  return (
    <tr className="home-recent-row" data-testid={`home-recent-${run.thread_id}`}>
      <td>
        <Link to={runPath} className="home-recent-link">
          <span className="strong">{run.title || 'Untitled run'}</span>
        </Link>
      </td>
      <td>{run.app_id ?? '—'}</td>
      <td>{run.current_phase ?? '—'}</td>
      <td>
        <span className={`status-badge ${statusTone(run.thread_status)}`}>
          {run.thread_status ?? 'unknown'}
        </span>
      </td>
      <td className="home-recent-time" title={run.updated_at ?? undefined}>
        {formatRelative(run.updated_at)}
      </td>
    </tr>
  )
}

function HomeSkeleton() {
  return (
    <div className="home-skeleton" role="status" aria-busy="true" aria-label="Loading dashboard">
      <div className="glass-panel home-skeleton-block" />
      <div className="glass-panel home-skeleton-block" />
      <div className="glass-panel home-skeleton-block tall" />
    </div>
  )
}

export function HomePage() {
  const topbarActions = useMemo(
    () => (
      <Link to="/runs/new" className="btn btn-primary">
        New Test
      </Link>
    ),
    [],
  )
  useTopbarContribution(topbarActions)

  const fleet = usePipelines(HOME_FLEET_FILTER)
  const inbox = useApprovalsInbox()
  const drafts = useDraftsList()
  const usage = useUsageAnalytics({})

  const items = fleet.data?.items ?? []
  const gates = inbox.items.slice(0, ATTENTION_GATES_LIMIT)
  const failures = failedRuns(items, items.length)
  const active = activeRuns(items)
  const recent = recentRuns(items)
  const draftItems = drafts.data ?? []
  const pendingApprovalsValue = inbox.isLoading || inbox.error ? '—' : inbox.count

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

  if (
    items.length === 0 &&
    !drafts.isPending &&
    !drafts.isError &&
    draftItems.length === 0
  ) {
    return (
      <section className="home-page animate-enter">
        <div className="dash-empty home-hero" data-testid="home-hero">
          <h2>Start your first pipeline</h2>
          <p className="dash-empty-hint">
            Launch a new test to populate health signals, approvals, and pipeline history.
          </p>
          <Link to="/runs/new" className="btn btn-primary">
            New Test
          </Link>
        </div>
      </section>
    )
  }

  const usageRuns = usage.data?.runs
  const loaded = totalRuns(items)
  const goCount = goVerdicts(items)
  const conditionalCount = interruptedRuns(items)
  const fleetScopeHint =
    loaded >= HOME_FLEET_LIMIT
      ? `latest ${HOME_FLEET_LIMIT}; fleet total unavailable`
      : 'loaded fleet snapshot'

  return (
    <section className="home-page animate-enter">
      <section className="home-metrics" aria-label="Key metrics">
        <MetricCard label="Runs Loaded" value={loaded} hint={fleetScopeHint} />
        <MetricCard label="Active" value={active.length} tone="warning" hint="in loaded runs" />
        <MetricCard label="Failures" value={failures.length} tone="danger" hint="in loaded runs" />
        <MetricCard
          label="GO Verdicts"
          value={goCount}
          tone="success"
          hint="successful outcomes loaded"
        />
        <MetricCard
          label="Avg Duration"
          value={avgDurationLabel(items)}
          tone="results"
          hint="loaded runs"
        />
        <MetricCard label="Tickets" value="—" hint="tracker count not exposed yet" />
      </section>

      <section className="home-signal-grid">
        <article className="glass-panel home-signal-card">
          <div className="home-section-head">
            <h3 className="home-section-title">Execution Health</h3>
            <Link to="/runs" className="home-view-all">
              Test History
            </Link>
          </div>
          <div className="home-health-grid">
            <div>
              <span className="home-health-stat-label">Active pipelines loaded</span>
              <strong className="home-health-stat-value">{active.length}</strong>
            </div>
            <div>
              <span className="home-health-stat-label">Pending approvals</span>
              <strong className="home-health-stat-value">{pendingApprovalsValue}</strong>
            </div>
            <div>
              <span className="home-health-stat-label">Phases succeeded</span>
              <strong className="home-health-stat-value">{usageRuns?.phases_succeeded ?? '—'}</strong>
            </div>
            <div>
              <span className="home-health-stat-label">Phases failed</span>
              <strong className="home-health-stat-value">{usageRuns?.phases_failed ?? '—'}</strong>
            </div>
          </div>
        </article>

        <article className="glass-panel home-signal-card">
          <div className="home-section-head">
            <h3 className="home-section-title">Release Signal</h3>
            <span className="home-signal-caption">loaded fleet snapshot proxy</span>
          </div>
          <div className="home-release-badges">
            <span className="topbar-meta-chip success">GO · {goCount}</span>
            <span className="topbar-meta-chip warning">Conditional · {conditionalCount}</span>
            <span className="topbar-meta-chip danger">NO-GO · {failures.length}</span>
          </div>
          <p className="home-release-note">
            Verdict API fields and fleet totals are not exposed in the current list endpoint, so
            this panel derives a conservative signal from phase outcomes in up to the latest{' '}
            {HOME_FLEET_LIMIT} loaded runs.
          </p>
        </article>
      </section>

      <section className="glass-panel home-approval-panel" aria-label="Awaiting HITL Approval">
        <div className="home-section-head">
          <h3 className="home-section-title">Awaiting HITL Approval</h3>
          <Link to="/approvals" className="home-view-all">
            Open queue
          </Link>
        </div>
        {inbox.isLoading ? (
          <div role="status" aria-busy="true">
            Loading pending approvals…
          </div>
        ) : inbox.error ? (
          <QueryErrorNotice
            title="Approvals unavailable"
            error={inbox.error}
            onRetry={inbox.refetch}
          />
        ) : (
          <>
            {inbox.refreshError && (
              <CachedDataWarning error={inbox.refreshError} onRetry={inbox.refetch} />
            )}
            <ApprovalQueue gates={gates} />
          </>
        )}
      </section>

      <section className="glass-panel home-recent" aria-label="Recent runs" data-testid="home-recent">
        <div className="home-section-head">
          <h3 className="home-section-title">Recent Runs</h3>
          <Link to="/runs" className="home-view-all">
            View all
          </Link>
        </div>
        {recent.length === 0 ? (
          <div className="dash-empty small">No runs yet.</div>
        ) : (
          <div className="data-table-wrap">
            <table className="data-table striped home-recent-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Application</th>
                  <th>Phase</th>
                  <th>Status</th>
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

      {(drafts.isError || draftItems.length > 0) && (
        <section className="glass-panel home-drafts" data-testid="home-drafts">
          <div className="home-section-head">
            <h3 className="home-section-title">Resume Drafts</h3>
          </div>
          {drafts.isError && !drafts.data ? (
            <QueryErrorNotice
              title="Saved drafts unavailable"
              error={drafts.error}
              onRetry={() => void drafts.refetch()}
            />
          ) : (
            <>
              {drafts.isError && drafts.data && (
                <CachedDataWarning
                  error={drafts.error}
                  onRetry={() => void drafts.refetch()}
                />
              )}
              <div className="home-draft-row-list">
                {draftItems.slice(0, 3).map((draft) => (
                  <Link key={draft.id} to={`/runs/new?draft=${draft.id}`} className="home-draft-row">
                    <span className="home-draft-title">{draft.title || 'Untitled draft'}</span>
                    <span className="home-draft-updated">{formatRelative(draft.updated_at)}</span>
                  </Link>
                ))}
              </div>
            </>
          )}
        </section>
      )}

      <section className="home-quick-nav">
        <Link to="/runs/new" className="btn btn-primary">
          New Test
        </Link>
        <Link to="/environments" className="btn btn-secondary">
          Env Configs
        </Link>
        <Link to="/runs" className="btn btn-secondary">
          Test History
        </Link>
        <Link to="/work-items" className="btn btn-secondary">
          Tickets
        </Link>
      </section>
    </section>
  )
}
