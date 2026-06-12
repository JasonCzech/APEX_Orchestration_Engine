import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useUsageAnalytics, type UsageAnalytics } from '@/api/hooks/useAnalytics'
import { isApiError } from '@/api/errors'
import { ProblemCard } from '@/components/ProblemCard'
import { WindowPresets } from '@/components/controls/WindowPresets'

import {
  BUCKETS,
  effectiveBucket,
  hasAnalyticsFilters,
  isBucket,
  parseAnalyticsFilters,
  serializeAnalyticsFilters,
  type AnalyticsFilters,
  type Bucket,
} from './analyticsFilters'
import './analytics.css'

const PROJECT_DEBOUNCE_MS = 300
const EM_DASH = '—'

/** Chart styling conventions follow EngineStrip (token-driven, animations off). */
const TOOLTIP_CONTENT_STYLE: React.CSSProperties = {
  background: 'var(--bg-elevated)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  color: 'var(--text-primary)',
  fontSize: 11,
}

const MONO_TICK = {
  fontSize: 10,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
} as const

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/** Bucket-start tick label: HH:mm for hourly histograms, short date for daily. */
function bucketTick(bucket: Bucket): (iso: string) => string {
  return (iso) => {
    const date = new Date(iso)
    if (Number.isNaN(date.getTime())) return iso
    return bucket === 'hour'
      ? `${pad(date.getHours())}:${pad(date.getMinutes())}`
      : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  }
}

function truncateAction(action: string, max = 24): string {
  return action.length > max ? `${action.slice(0, max - 1)}…` : action
}

function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return 'Usage analytics could not be loaded.'
}

function StatCard({
  label,
  value,
  hint,
  tone,
  testId,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'danger'
  testId: string
}) {
  return (
    <div className={`glass-panel stat-card${tone ? ` ${tone}` : ''}`} data-testid={testId}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  )
}

function AnalyticsSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading analytics">
      <div className="stat-cards">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="glass-panel stat-card analytics-skeleton-card" />
        ))}
      </div>
      <div className="analytics-charts">
        <div className="glass-panel chart-panel analytics-skeleton-chart" />
        <div className="glass-panel chart-panel analytics-skeleton-chart" />
      </div>
    </div>
  )
}

function UsageCharts({ data, bucket }: { data: UsageAnalytics; bucket: Bucket }) {
  const series = useMemo(
    () =>
      data.buckets.map((entry) => ({
        bucket_start: entry.bucket_start,
        events: entry.events,
        errors: entry.errors,
      })),
    [data.buckets],
  )
  const actions = useMemo(
    () =>
      // Server caps at 10; slice defensively and truncate labels for the axis.
      data.top_actions.slice(0, 10).map((entry) => ({
        action: entry.action,
        label: truncateAction(entry.action),
        count: entry.count,
      })),
    [data.top_actions],
  )

  return (
    <div className="analytics-charts">
      <section className="glass-panel chart-panel" data-testid="analytics-events-chart">
        <h2 className="chart-title">Events over time</h2>
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={series} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="bucket_start"
              stroke="var(--text-muted)"
              tick={{ fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              minTickGap={32}
              tickFormatter={bucketTick(bucket)}
            />
            <YAxis
              stroke="var(--text-muted)"
              tick={{ fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              width={44}
              allowDecimals={false}
            />
            <Tooltip
              contentStyle={TOOLTIP_CONTENT_STYLE}
              isAnimationActive={false}
              labelFormatter={(iso) => bucketTick(bucket)(String(iso))}
            />
            <Area
              type="monotone"
              dataKey="events"
              name="events"
              stroke="var(--chart-1)"
              fill="var(--chart-1)"
              fillOpacity={0.16}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
            <Area
              type="monotone"
              dataKey="errors"
              name="errors"
              stroke="var(--danger)"
              fill="var(--danger)"
              fillOpacity={0.12}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </section>

      <section className="glass-panel chart-panel" data-testid="analytics-actions-chart">
        <h2 className="chart-title">Top actions</h2>
        {actions.length === 0 ? (
          <div className="dash-empty compact">
            <p className="dash-empty-hint">No actions recorded in this window.</p>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={Math.max(160, actions.length * 28 + 40)}>
            <BarChart
              data={actions}
              layout="vertical"
              margin={{ top: 8, right: 16, bottom: 0, left: 8 }}
            >
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" horizontal={false} />
              <XAxis
                type="number"
                stroke="var(--text-muted)"
                tick={{ fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                allowDecimals={false}
              />
              <YAxis
                type="category"
                dataKey="label"
                stroke="var(--text-muted)"
                tick={MONO_TICK}
                tickLine={false}
                axisLine={false}
                width={150}
                interval={0}
              />
              <Tooltip
                contentStyle={TOOLTIP_CONTENT_STYLE}
                isAnimationActive={false}
                cursor={{ fill: 'var(--bg-hover)' }}
              />
              <Bar
                dataKey="count"
                name="events"
                fill="var(--chart-2)"
                radius={[0, 4, 4, 0]}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        )}
      </section>
    </div>
  )
}

/**
 * /analytics — usage analytics over GET /v1/analytics/usage (plan Part 2).
 * All filters round-trip through the URL (?from&to&bucket&project, runs-grid
 * pattern); window presets write absolute from/to so links stay shareable.
 */
export function AnalyticsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => parseAnalyticsFilters(searchParams), [searchParams])
  const bucket = effectiveBucket(filters)

  const applyFilters = useCallback(
    (patch: Partial<AnalyticsFilters>) => {
      setSearchParams((prev) =>
        serializeAnalyticsFilters({ ...parseAnalyticsFilters(prev), ...patch }),
      )
    },
    [setSearchParams],
  )

  // Project filter: local echo, committed to the URL after a 300ms debounce.
  const [project, setProject] = useState(filters.project ?? '')
  const committedProject = filters.project ?? ''
  useEffect(() => {
    setProject(committedProject)
  }, [committedProject])
  useEffect(() => {
    const trimmed = project.trim()
    if (trimmed === committedProject) return undefined
    const id = window.setTimeout(() => {
      applyFilters({ project: trimmed || undefined })
    }, PROJECT_DEBOUNCE_MS)
    return () => window.clearTimeout(id)
  }, [project, committedProject, applyFilters])

  const { data, error, isPending, isError, refetch } = useUsageAnalytics({
    ...(filters.from ? { from: filters.from } : {}),
    ...(filters.to ? { to: filters.to } : {}),
    bucket,
    ...(filters.project ? { project: filters.project } : {}),
  })

  const totals = data?.totals
  const isEmptyWindow =
    data !== undefined && totals !== undefined && totals.events === 0 && totals.errors === 0
  const errorRate =
    totals && totals.events > 0 ? `${((totals.errors / totals.events) * 100).toFixed(1)}%` : EM_DASH
  const surfaces = Object.entries(data?.totals.by_surface ?? {})

  return (
    <section className="analytics-page animate-enter">
      <header className="analytics-toolbar glass-panel">
        <WindowPresets
          value={{ ...(filters.from ? { from: filters.from } : {}), ...(filters.to ? { to: filters.to } : {}) }}
          onChange={(window) => applyFilters({ from: window.from, to: window.to })}
        />
        <select
          className="field-select"
          aria-label="Histogram bucket"
          value={filters.bucket ?? ''}
          onChange={(event) => {
            const value = event.target.value
            applyFilters({ bucket: isBucket(value) ? value : undefined })
          }}
        >
          <option value="">Auto ({bucket})</option>
          {BUCKETS.map((candidate) => (
            <option key={candidate} value={candidate}>
              {candidate}
            </option>
          ))}
        </select>
        <input
          type="search"
          className="field-input analytics-project"
          placeholder="Filter by project…"
          aria-label="Filter by project"
          value={project}
          onChange={(event) => setProject(event.target.value)}
        />
        {hasAnalyticsFilters(filters) && (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => {
              setProject('')
              setSearchParams(new URLSearchParams())
            }}
          >
            Clear filters
          </button>
        )}
      </header>

      {isPending ? (
        <AnalyticsSkeleton />
      ) : isError && !data ? (
        <ProblemCard
          title="Analytics unavailable"
          message={errorMessage(error)}
          onRetry={() => refetch()}
        />
      ) : data ? (
        <>
          <div className="stat-cards" data-testid="analytics-cards">
            <StatCard
              label="Events"
              value={data.totals.events.toLocaleString()}
              testId="stat-events"
            />
            <StatCard
              label="Errors"
              value={data.totals.errors.toLocaleString()}
              testId="stat-errors"
              {...(data.totals.errors > 0 ? { tone: 'danger' as const } : {})}
            />
            <StatCard label="Error rate" value={errorRate} testId="stat-error-rate" />
            <StatCard
              label="Phases"
              value={`${data.runs.phases_succeeded} / ${data.runs.phases_failed}`}
              hint="succeeded / failed"
              testId="stat-phases"
            />
          </div>

          {surfaces.length > 0 && (
            <div className="surface-chips" data-testid="surface-chips">
              {surfaces.map(([surface, count]) => (
                <span key={surface} className="topbar-meta-chip">
                  {surface}: {count.toLocaleString()}
                </span>
              ))}
            </div>
          )}

          {isEmptyWindow ? (
            <div className="dash-empty">
              <h2>No usage in this window</h2>
              <p className="dash-empty-hint">
                Try a wider time window or clear the project filter.
              </p>
            </div>
          ) : (
            <UsageCharts data={data} bucket={data.window.bucket} />
          )}
        </>
      ) : null}
    </section>
  )
}
