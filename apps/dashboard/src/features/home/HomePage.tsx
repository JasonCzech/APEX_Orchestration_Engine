import { useTimestampPreference } from '@/theme/preferences'
import { ApplicationLabel } from '@/components/ux/ApplicationLabel'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { orchestrationTone, formatTimestamp, executionResultLabel } from '@/features/runs/runDisplay'
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
import { gateLabel, orchestrationStatusLabel, isPhaseName, PHASE_LABELS } from '@/features/runs/runDisplay'
import { formatRelative } from '@/utils/time'

import {
  ATTENTION_GATES_LIMIT,
  HOME_FLEET_FILTER,
  HOME_FLEET_LIMIT,
  activeRuns,
  avgDurationLabel,
  failedRuns,
  gateHref,
  resultCounts,
  recentRuns,
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
      <StateCard kind="empty" title="No pending reviews right now." action={null} compact />
    )
  }

  return (
    <ul className="home-approval-list" data-testid="home-approvals-list">
      {gates.map((gate) => (
        <li key={gate.thread_id}>
          <Link to={gateHref(gate.thread_id, gate.pending_gate.interrupt_id)} className="home-approval-row">
            <span className="topbar-meta-chip warning">{gateLabel(gate.pending_gate.kind)}</span>
            <span className="home-approval-title">{gate.title}</span>
            <span className="home-approval-phase">{isPhaseName(gate.pending_gate.phase) ? PHASE_LABELS[gate.pending_gate.phase] : 'Phase not recorded'}</span>
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
      <td><ApplicationLabel project={run.project_id} application={run.app_id} empty="Not recorded" /></td>
      <td>{isPhaseName(run.current_phase) ? PHASE_LABELS[run.current_phase] : '—'}</td>
      <td>
        <span className={`status-badge ${orchestrationTone(run.thread_status, run.phase_strip)}`}>
          {orchestrationStatusLabel(run.thread_status, run.phase_strip)}
        </span>
      </td>
      <td>{executionResultLabel(run)}</td>
      <td className="home-recent-time" title={run.updated_at ?? undefined}>
        {formatTimestamp(run.updated_at)}
      </td>
    </tr>
  )
}

function HomeSkeleton() {
  return <Skeleton label="Loading dashboard" rows={3} />
}

export function HomePage() {
  useTimestampPreference()
  const topbarActions = useMemo(
    () => (
      <Link to="/runs/new" className="btn btn-primary">
        New test
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
        <div data-testid="home-hero">
          <StateCard kind="empty" title="Start your first test" action={<Link to="/runs/new" className="btn btn-primary">New test</Link>}>
            <p>Launch a new test to populate health signals, reviews, and run history.</p>
          </StateCard>
        </div>
      </section>
    )
  }

  const usageRuns = usage.data?.runs
  const loaded = totalRuns(items)
  const results = resultCounts(items)
  const fleetScopeHint =
    loaded >= HOME_FLEET_LIMIT
      ? `latest ${HOME_FLEET_LIMIT}; fleet total unavailable`
      : 'recent accessible runs'

  return (
    <section className="home-page animate-enter">
      <section className="glass-panel home-approval-panel" aria-label="Needs your attention">
        <div className="home-section-head">
          <h2 className="home-section-title">Needs your attention</h2>
          <Link to="/approvals" className="home-view-all">
            Open queue
          </Link>
        </div>
        {inbox.isLoading ? (
          <Skeleton label="Loading pending reviews" rows={2} />
        ) : inbox.error ? (
          <QueryErrorNotice
            title="Reviews unavailable"
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


      <section className="home-metrics" aria-label="Key metrics">
        <MetricCard label="Recent runs" value={loaded} hint={fleetScopeHint} />
        <MetricCard label="Active" value={active.length} tone="warning" hint="running or awaiting review, in loaded runs" />
        <MetricCard label="Runs needing investigation" value={failures.length} tone="danger" hint="test failures or orchestration errors in loaded runs" />
        <MetricCard
          label="Avg elapsed snapshot"
          value={avgDurationLabel(items)}
          tone="results"
          hint="loaded runs: creation to last update, not execution duration"
        />
      </section>

      <section className="home-signal-grid">
        <article className="glass-panel home-signal-card">
          <div className="home-section-head">
            <h2 className="home-section-title">Execution health</h2>
            <Link to="/runs" className="home-view-all">
              Run history
            </Link>
          </div>
          <p className="wizard-caption">{usage.data?.window ? `${formatTimestamp(usage.data.window.from)} – ${formatTimestamp(usage.data.window.to)}, across accessible projects.` : 'Reporting window not available.'} Refreshed {usage.dataUpdatedAt ? formatTimestamp(new Date(usage.dataUpdatedAt).toISOString()) : 'not yet'}. Only succeeded and failed phases are counted here; skipped, aborted, waiting and active phases are excluded. A dash means no summary is available.</p>
          <div className="home-health-grid">
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
            <h2 className="home-section-title">Test results</h2>
            <span className="home-signal-caption">loaded runs only</span>
          </div>
          <div className="home-release-badges">
            {Object.entries(results).map(([label, count]) => <span key={label} className={`topbar-meta-chip ${label === 'Passed' ? 'success' : label === 'Failed' ? 'danger' : 'warning'}`}>{label} · {count}</span>)}
          </div>
          <p className="home-release-note">
            Recorded execution outcomes in up to the latest {HOME_FLEET_LIMIT} loaded runs.
            These results describe test thresholds, not release certification.
          </p>
        </article>
      </section>


      <section className="glass-panel home-recent" aria-label="Recent runs" data-testid="home-recent">
        <div className="home-section-head">
          <h2 className="home-section-title">Recent runs</h2>
          <Link to="/runs" className="home-view-all">
            View all
          </Link>
        </div>
        {recent.length === 0 ? (
          <StateCard kind="empty" title="No runs yet." action={<Link to="/runs/new" className="btn btn-primary">New test</Link>} compact />
        ) : (
          <div className="data-table-wrap">
            <table className="data-table striped home-recent-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Application</th>
                  <th>Phase</th>
                  <th>Status</th>
                  <th>Test result</th>
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
            <h2 className="home-section-title">Resume drafts</h2>
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
                    <span className="home-draft-title"><span>{draft.title || 'Untitled draft'}</span>{draft.project_id ? ` · ${draft.project_id}` : ''}</span>
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
          New test
        </Link>
        <Link to="/environments" className="btn btn-secondary">
          Applications &amp; environments
        </Link>
        <Link to="/runs" className="btn btn-secondary">
          Run history
        </Link>
        <Link to="/work-items" className="btn btn-secondary">
          Tickets
        </Link>
      </section>
    </section>
  )
}
