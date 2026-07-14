import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { PHASE_NAMES } from '@apex/pipeline-events'

import {
  useAgentAnalytics,
  type AgentAnalytics,
  type AgentAnalyticsBreakdownRow,
  type AgentAnalyticsSeriesPoint,
  type AgentOrder,
  type AgentSort,
} from '@/api/hooks/useAgentAnalytics'
import { isApiError } from '@/api/errors'
import { ProblemCard } from '@/components/ProblemCard'
import { WindowPresets } from '@/components/controls/WindowPresets'

import {
  DEFAULT_GROUP,
  DEFAULT_LIMIT,
  DEFAULT_MEASURE,
  GROUP_BYS,
  MEASURES,
  defaultSortFor,
  effectiveBucket,
  hasAnalyticsFilters,
  parseAnalyticsFilters,
  serializeAnalyticsFilters,
  type AnalyticsFilters,
  type Bucket,
  type GroupBy,
  type Measure,
} from './analyticsFilters'
import { formatCost, formatLatency, formatPercent, formatTokens } from './analyticsFormat'
import './analytics.css'

const TEXT_DEBOUNCE_MS = 300
const CHART_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
  'var(--chart-6)',
] as const
const MODEL_OPTIONS = [
  'claude-3-5-sonnet-latest',
  'claude-3-5-haiku-latest',
  'gpt-4o',
  'gpt-4o-mini',
]
/** Bar chart shows the top N breakdown rows; the table page size can exceed this. */
const TOP_N_BARS = 10
const AGENT_OPTIONS = PHASE_NAMES.map((phase) => `${phase}.worker`)

const TOOLTIP_CONTENT_STYLE: React.CSSProperties = {
  background: 'var(--bg-elevated)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  color: 'var(--text-primary)',
  fontSize: 11,
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function bucketTick(bucket: Bucket): (iso: string) => string {
  return (iso) => {
    const date = new Date(iso)
    if (Number.isNaN(date.getTime())) return iso
    return bucket === 'hour'
      ? `${pad(date.getHours())}:${pad(date.getMinutes())}`
      : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  }
}

function truncate(value: string, max = 24): string {
  return value.length > max ? `${value.slice(0, max - 1)}...` : value
}

function labelForGroup(group: GroupBy): string {
  return {
    model: 'Model',
    stage: 'Stage',
    agent: 'Worker agent',
    test: 'Test',
    date: 'Date',
  }[group]
}

function labelForMeasure(measure: Measure): string {
  return { tokens: 'Tokens', cost: 'Cost', latency: 'Latency' }[measure]
}

function metricValue(row: AgentAnalyticsBreakdownRow | AgentAnalyticsSeriesPoint, measure: Measure): number {
  if (measure === 'cost') return row.cost_usd ?? 0
  if (measure === 'latency') return row.avg_latency_ms ?? row.p95_latency_ms ?? 0
  return row.total_tokens
}

function formatMeasure(value: number | null | undefined, measure: Measure): string {
  if (measure === 'cost') return formatCost(value)
  if (measure === 'latency') return formatLatency(value)
  return formatTokens(value ?? undefined)
}

function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return 'Agent analytics could not be loaded.'
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
    <div role="status" aria-busy="true" aria-label="Loading agent analytics">
      <div className="stat-cards">
        {Array.from({ length: 5 }, (_, i) => (
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

function SegmentControl<T extends string>({
  label,
  value,
  options,
  labels,
  disabled,
  onChange,
}: {
  label: string
  value: T
  options: readonly T[]
  labels: Record<T, string>
  disabled?: Partial<Record<T, boolean>>
  onChange: (value: T) => void
}) {
  return (
    <div className="analytics-control-group">
      <span className="analytics-control-label">{label}</span>
      <div className="segmented" role="group" aria-label={label}>
        {options.map((option) => (
          <button
            key={option}
            type="button"
            className="segmented-btn"
            aria-pressed={value === option}
            disabled={disabled?.[option]}
            onClick={() => onChange(option)}
          >
            {labels[option]}
          </button>
        ))}
      </div>
    </div>
  )
}

function ChipGroup({
  label,
  options,
  selected,
  onToggle,
}: {
  label: string
  options: readonly string[]
  selected: readonly string[]
  onToggle: (value: string) => void
}) {
  return (
    <div className="analytics-control-group">
      <span className="analytics-control-label">{label}</span>
      <div className="level-chips analytics-chip-row" role="group" aria-label={`${label} filter`}>
        {options.map((option) => (
          <button
            key={option}
            type="button"
            className="level-chip analytics-chip"
            aria-pressed={selected.includes(option)}
            onClick={() => onToggle(option)}
          >
            {truncate(option, 20)}
          </button>
        ))}
      </div>
    </div>
  )
}

function toggleValue(values: string[] | undefined, value: string): string[] | undefined {
  const current = values ?? []
  const next = current.includes(value)
    ? current.filter((entry) => entry !== value)
    : [...current, value]
  return next.length ? next : undefined
}

function agentQuery(filters: AnalyticsFilters, group: GroupBy, measure: Measure, bucket: Bucket) {
  return {
    ...(filters.from ? { from: filters.from } : {}),
    ...(filters.to ? { to: filters.to } : {}),
    bucket,
    group_by: group,
    ...(filters.project ? { project: filters.project } : {}),
    ...(filters.model?.length ? { model: filters.model } : {}),
    ...(filters.stage?.length ? { stage: filters.stage } : {}),
    ...(filters.agent?.length ? { agent: filters.agent } : {}),
    ...(filters.test ? { test: filters.test } : {}),
    ...(filters.status ? { status: filters.status } : {}),
    sort: filters.sort ?? defaultSortFor(measure),
    order: filters.dir ?? 'desc',
    limit: DEFAULT_LIMIT,
    offset: filters.offset ?? 0,
  }
}

function seriesRows(data: AgentAnalytics, measure: Measure) {
  // `breakdown` is the currently requested table page, while `series` is the
  // server-selected chart population. Deriving legend keys from breakdown
  // makes every page after offset 0 render empty/mislabeled series. Rank the
  // keys from the series payload itself so chart identity is independent of
  // table pagination.
  const totalsByKey = new Map<string, number>()
  const countsByKey = new Map<string, number>()
  for (const row of data.series) {
    totalsByKey.set(row.key, (totalsByKey.get(row.key) ?? 0) + metricValue(row, measure))
    countsByKey.set(row.key, (countsByKey.get(row.key) ?? 0) + 1)
  }
  if (measure === 'latency') {
    for (const [key, total] of totalsByKey) totalsByKey.set(key, total / (countsByKey.get(key) ?? 1))
  }
  const rankedKeys = Array.from(totalsByKey, ([key, value]) => ({ key, value }))
    .sort((a, b) => b.value - a.value || a.key.localeCompare(b.key))
    .map(({ key }) => key)
  const keys = rankedKeys.slice(0, 5)
  const rest = rankedKeys.slice(5)
  const buckets = new Map<string, Record<string, number | string>>()
  for (const row of data.series) {
    const bucket = row.bucket_start
    const target = buckets.get(bucket) ?? { bucket_start: bucket }
    if (keys.includes(row.key)) {
      const field = `series_${keys.indexOf(row.key)}`
      target[field] = measure === 'latency'
        ? metricValue(row, measure)
        : Number(target[field] ?? 0) + metricValue(row, measure)
    } else if (rest.includes(row.key)) {
      target.other = measure === 'latency'
        ? metricValue(row, measure)
        : Number(target.other ?? 0) + metricValue(row, measure)
    }
    buckets.set(bucket, target)
  }
  const series = Array.from(buckets.values()).sort((a, b) =>
    String(a.bucket_start).localeCompare(String(b.bucket_start)),
  )
  return {
    keys: rest.length ? [...keys, 'Other'] : keys,
    fields: rest.length ? [...keys.map((_, i) => `series_${i}`), 'other'] : keys.map((_, i) => `series_${i}`),
    series,
  }
}

function AgentKpiCards({
  data,
  allZeroTokens,
}: {
  data: AgentAnalytics
  allZeroTokens: boolean
}) {
  const errorRate = formatPercent(data.totals.errors, data.totals.events)
  return (
    <div className="stat-cards" data-testid="analytics-cards">
      <StatCard
        label="Total tokens"
        value={allZeroTokens ? '—' : formatTokens(data.totals.total_tokens)}
        hint={allZeroTokens ? 'awaiting live LLM agents' : `${formatTokens(data.totals.input_tokens)} in / ${formatTokens(data.totals.output_tokens)} out`}
        testId="stat-total-tokens"
      />
      {data.cost_visible && (
        <StatCard label="Est. cost" value={formatCost(data.totals.cost_usd)} testId="stat-cost" />
      )}
      <StatCard
        label="Avg latency"
        value={formatLatency(data.totals.avg_latency_ms)}
        hint={`p95 ${formatLatency(data.totals.p95_latency_ms)}`}
        testId="stat-latency"
      />
      <StatCard
        label="Agents / runs"
        value={`${data.totals.agents} / ${data.totals.runs}`}
        hint={`${data.totals.models} models`}
        testId="stat-agents-runs"
      />
      <StatCard
        label="Error rate"
        value={errorRate}
        testId="stat-error-rate"
        {...(data.totals.errors > 0 ? { tone: 'danger' as const } : {})}
      />
    </div>
  )
}

function AgentUsageCharts({
  data,
  group,
  measure,
  bucket,
}: {
  data: AgentAnalytics
  group: GroupBy
  measure: Measure
  bucket: Bucket
}) {
  const stacked = useMemo(() => seriesRows(data, measure), [data, measure])
  const bars = useMemo(() => {
    const grouped = new Map<string, number[]>()
    for (const row of data.series) {
      const values = grouped.get(row.key) ?? []
      values.push(metricValue(row, measure))
      grouped.set(row.key, values)
    }
    return Array.from(grouped, ([key, values]) => ({
      key,
      label: truncate(key),
      value: measure === 'latency'
        ? values.reduce((sum, value) => sum + value, 0) / values.length
        : values.reduce((sum, value) => sum + value, 0),
    }))
      .sort((a, b) => b.value - a.value || a.key.localeCompare(b.key))
      .slice(0, TOP_N_BARS)
  }, [data.series, measure])

  return (
    <div className="analytics-charts">
      <section className="glass-panel chart-panel" data-testid="analytics-agent-series-chart">
        <h2 className="chart-title">
          {labelForMeasure(measure)} over time by {labelForGroup(group).toLowerCase()}
        </h2>
        <ResponsiveContainer width="100%" height={240}>
          <AreaChart data={stacked.series} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
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
              width={54}
              tickFormatter={(value) => formatMeasure(Number(value), measure)}
            />
            <Tooltip
              contentStyle={TOOLTIP_CONTENT_STYLE}
              isAnimationActive={false}
              labelFormatter={(iso) => bucketTick(bucket)(String(iso))}
              formatter={(value, name) => [formatMeasure(Number(value), measure), name]}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {stacked.fields.map((field, index) => (
              <Area
                key={field}
                type="monotone"
                dataKey={field}
                name={stacked.keys[index]}
                stackId={measure === 'latency' ? undefined : 'agent'}
                stroke={CHART_COLORS[index % CHART_COLORS.length]}
                fill={CHART_COLORS[index % CHART_COLORS.length]}
                fillOpacity={0.16}
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </section>

      <section className="glass-panel chart-panel" data-testid="analytics-agent-top-chart">
        <h2 className="chart-title">
          Top {bars.length} {labelForGroup(group).toLowerCase()} by {labelForMeasure(measure).toLowerCase()}
        </h2>
        <ResponsiveContainer width="100%" height={Math.max(180, bars.length * 30 + 48)}>
          <BarChart data={bars} layout="vertical" margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" horizontal={false} />
            <XAxis
              type="number"
              stroke="var(--text-muted)"
              tick={{ fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              tickFormatter={(value) => formatMeasure(Number(value), measure)}
            />
            <YAxis
              type="category"
              dataKey="label"
              stroke="var(--text-muted)"
              tick={{ fontSize: 10, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}
              tickLine={false}
              axisLine={false}
              width={150}
              interval={0}
            />
            <Tooltip
              contentStyle={TOOLTIP_CONTENT_STYLE}
              isAnimationActive={false}
              cursor={{ fill: 'var(--bg-hover)' }}
              formatter={(value) => [formatMeasure(Number(value), measure), labelForMeasure(measure)]}
            />
            <Bar
              dataKey="value"
              name={labelForMeasure(measure)}
              fill="var(--chart-2)"
              radius={[0, 4, 4, 0]}
              isAnimationActive={false}
            />
          </BarChart>
        </ResponsiveContainer>
      </section>
    </div>
  )
}

function TokenSplitCharts({ data }: { data: AgentAnalytics }) {
  const rows = useMemo(() => {
    const buckets = new Map<string, { bucket_start: string; input: number; output: number; cost: number }>()
    for (const row of data.series) {
      const bucket = buckets.get(row.bucket_start) ?? {
        bucket_start: row.bucket_start,
        input: 0,
        output: 0,
        cost: 0,
      }
      bucket.input += row.input_tokens
      bucket.output += row.output_tokens
      bucket.cost += row.cost_usd ?? 0
      buckets.set(row.bucket_start, bucket)
    }
    return Array.from(buckets.values()).sort((a, b) => a.bucket_start.localeCompare(b.bucket_start))
  }, [data.series])

  return (
    <div className="analytics-charts analytics-charts-secondary">
      <section className="glass-panel chart-panel" data-testid="analytics-token-split-chart">
        <h2 className="chart-title">Input vs output tokens</h2>
        <ResponsiveContainer width="100%" height={200}>
          <AreaChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="bucket_start" tickFormatter={bucketTick(data.window.bucket)} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} />
            <YAxis tickFormatter={(value) => formatTokens(Number(value))} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} width={54} />
            <Tooltip contentStyle={TOOLTIP_CONTENT_STYLE} isAnimationActive={false} formatter={(value) => formatTokens(Number(value))} />
            <Area type="monotone" dataKey="input" name="input" stroke="var(--chart-1)" fill="var(--chart-1)" fillOpacity={0.16} dot={false} isAnimationActive={false} />
            <Area type="monotone" dataKey="output" name="output" stroke="var(--chart-5)" fill="var(--chart-5)" fillOpacity={0.12} dot={false} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </section>
      {data.cost_visible && (
        <section className="glass-panel chart-panel" data-testid="analytics-cost-trend-chart">
          <h2 className="chart-title">Cost trend</h2>
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="bucket_start" tickFormatter={bucketTick(data.window.bucket)} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} />
              <YAxis tickFormatter={(value) => formatCost(Number(value))} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} width={54} />
              <Tooltip contentStyle={TOOLTIP_CONTENT_STYLE} isAnimationActive={false} formatter={(value) => formatCost(Number(value))} />
              <Area type="monotone" dataKey="cost" name="cost" stroke="var(--chart-3)" fill="var(--chart-3)" fillOpacity={0.14} dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </section>
      )}
    </div>
  )
}

function nextSort(current: AgentSort, currentDir: AgentOrder, next: AgentSort): AgentOrder {
  if (current !== next) return 'desc'
  return currentDir === 'desc' ? 'asc' : 'desc'
}

function sortState(active: boolean, dir: AgentOrder): 'ascending' | 'descending' | 'none' {
  if (!active) return 'none'
  return dir === 'asc' ? 'ascending' : 'descending'
}

function AgentBreakdownTable({
  data,
  group,
  sort,
  dir,
  onSort,
  onFilter,
  onZoomDate,
  onPage,
}: {
  data: AgentAnalytics
  group: GroupBy
  sort: AgentSort
  dir: AgentOrder
  onSort: (sort: AgentSort) => void
  onFilter: (group: GroupBy, value: string) => void
  onZoomDate: (iso: string) => void
  onPage: (offset: number) => void
}) {
  const page = data.page
  const prevDisabled = page.offset <= 0
  const nextDisabled = page.offset + page.limit >= page.total
  const caption =
    page.total === 0
      ? 'No rows'
      : `${page.offset + 1}-${Math.min(page.offset + data.breakdown.length, page.total)} of ${page.total}`

  function SortHead({ id, children }: { id: AgentSort; children: React.ReactNode }) {
    const active = sort === id
    return (
      <th scope="col" aria-sort={sortState(active, dir)}>
        <button type="button" className="analytics-sort" onClick={() => onSort(id)}>
          {children}
          <span aria-hidden="true">{active ? (dir === 'asc' ? '↑' : '↓') : ''}</span>
        </button>
      </th>
    )
  }

  return (
    <>
      <div className="data-table-wrap">
        <table className="data-table striped analytics-table" data-testid="analytics-breakdown-table">
          <thead>
            <tr>
              <SortHead id="key">{labelForGroup(group)}</SortHead>
              <SortHead id="runs">Runs</SortHead>
              <SortHead id="total_tokens">Total tokens</SortHead>
              <SortHead id="input_tokens">In / out</SortHead>
              {data.cost_visible && <SortHead id="cost_usd">Est. cost</SortHead>}
              <SortHead id="avg_latency_ms">Avg latency</SortHead>
              <SortHead id="p95_latency_ms">p95</SortHead>
              <SortHead id="errors">Errors</SortHead>
            </tr>
          </thead>
          <tbody>
            {data.breakdown.map((row) => (
              <tr key={row.key}>
                <td className="strong analytics-key-cell">{renderKeyCell(row, group, onFilter, onZoomDate)}</td>
                <td className="num">{row.runs.toLocaleString()}</td>
                <td className="num">{formatTokens(row.total_tokens)}</td>
                <td className="num">{formatTokens(row.input_tokens)} / {formatTokens(row.output_tokens)}</td>
                {data.cost_visible && <td className="num">{formatCost(row.cost_usd)}</td>}
                <td className="num">{formatLatency(row.avg_latency_ms)}</td>
                <td className="num">{formatLatency(row.p95_latency_ms)}</td>
                <td className="num">{row.errors.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <footer className="analytics-pagination">
        <span className="analytics-pagination-caption">{caption}</span>
        <div className="analytics-pagination-buttons">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={prevDisabled}
            onClick={() => onPage(Math.max(0, page.offset - page.limit))}
          >
            Previous
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={nextDisabled}
            onClick={() => onPage(page.offset + page.limit)}
          >
            Next
          </button>
        </div>
      </footer>
    </>
  )
}

function renderKeyCell(
  row: AgentAnalyticsBreakdownRow,
  group: GroupBy,
  onFilter: (group: GroupBy, value: string) => void,
  onZoomDate: (iso: string) => void,
) {
  if (group === 'test') {
    return <Link to={`/runs/${row.thread_id ?? row.key}`}>{row.key}</Link>
  }
  if (group === 'date') {
    return (
      <button type="button" className="analytics-link-button" onClick={() => onZoomDate(row.key)}>
        {new Date(row.key).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
      </button>
    )
  }
  return (
    <button type="button" className="analytics-link-button" onClick={() => onFilter(group, row.key)}>
      {row.key}
    </button>
  )
}

export function AnalyticsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => parseAnalyticsFilters(searchParams), [searchParams])
  const group = filters.group ?? DEFAULT_GROUP
  const selectedMeasure = filters.measure ?? DEFAULT_MEASURE
  const bucket = effectiveBucket(filters)
  const sort = filters.sort ?? defaultSortFor(selectedMeasure)
  const dir = filters.dir ?? 'desc'

  const applyFilters = useCallback(
    (patch: Partial<AnalyticsFilters>) => {
      setSearchParams((prev) =>
        serializeAnalyticsFilters({ ...parseAnalyticsFilters(prev), ...patch }),
      )
    },
    [setSearchParams],
  )

  const [project, setProject] = useState(filters.project ?? '')
  const [test, setTest] = useState(filters.test ?? '')
  useEffect(() => setProject(filters.project ?? ''), [filters.project])
  useEffect(() => setTest(filters.test ?? ''), [filters.test])
  useEffect(() => {
    const trimmed = project.trim()
    if (trimmed === (filters.project ?? '')) return undefined
    const id = window.setTimeout(
      () => applyFilters({ project: trimmed || undefined, offset: undefined }),
      TEXT_DEBOUNCE_MS,
    )
    return () => window.clearTimeout(id)
  }, [project, filters.project, applyFilters])
  useEffect(() => {
    const trimmed = test.trim()
    if (trimmed === (filters.test ?? '')) return undefined
    const id = window.setTimeout(
      () => applyFilters({ test: trimmed || undefined, offset: undefined }),
      TEXT_DEBOUNCE_MS,
    )
    return () => window.clearTimeout(id)
  }, [test, filters.test, applyFilters])

  const query = useMemo(
    () => agentQuery(filters, group, selectedMeasure, bucket),
    [filters, group, selectedMeasure, bucket],
  )
  const { data, error, isPending, isError, refetch } = useAgentAnalytics(query)

  const allZeroTokens = data !== undefined && data.totals.events > 0 && data.totals.total_tokens === 0
  const effectiveMeasure: Measure =
    (selectedMeasure === 'cost' && data?.cost_visible !== true) ||
    (allZeroTokens && selectedMeasure !== 'latency')
      ? 'latency'
      : selectedMeasure
  const isEmptyWindow = data !== undefined && data.totals.events === 0

  // Self-correct a deep-linked cost measure/sort the server won't expose to this
  // caller, so the table and query never sort by a hidden column (review R3).
  useEffect(() => {
    if (!data || data.cost_visible) return
    const dropMeasure = selectedMeasure === 'cost'
    const dropSort = filters.sort === 'cost_usd'
    if (!dropMeasure && !dropSort) return
    setSearchParams(
      (prev) => {
        const next = parseAnalyticsFilters(prev)
        if (dropMeasure) delete next.measure
        if (dropSort) {
          delete next.sort
          delete next.dir
        }
        return serializeAnalyticsFilters(next)
      },
      { replace: true },
    )
  }, [data, filters.sort, selectedMeasure, setSearchParams])

  function setGroup(next: GroupBy) {
    applyFilters({ group: next, offset: undefined })
  }

  function setMeasure(next: Measure) {
    applyFilters({ measure: next, sort: defaultSortFor(next), dir: 'desc', offset: undefined })
  }

  function onSort(next: AgentSort) {
    applyFilters({ sort: next, dir: nextSort(sort, dir, next), offset: undefined })
  }

  function addBreakdownFilter(targetGroup: GroupBy, value: string) {
    if (targetGroup === 'model') applyFilters({ model: toggleValue(filters.model, value), offset: undefined })
    if (targetGroup === 'stage') applyFilters({ stage: toggleValue(filters.stage, value), offset: undefined })
    if (targetGroup === 'agent') applyFilters({ agent: toggleValue(filters.agent, value), offset: undefined })
  }

  function zoomDate(iso: string) {
    const from = new Date(iso)
    if (Number.isNaN(from.getTime())) return
    const to = new Date(from.getTime() + (bucket === 'hour' ? 60 * 60_000 : 24 * 60 * 60_000))
    applyFilters({ from: from.toISOString(), to: to.toISOString(), offset: undefined })
  }

  return (
    <section className="analytics-page animate-enter">
      <header className="analytics-toolbar glass-panel">
        <WindowPresets
          value={{ ...(filters.from ? { from: filters.from } : {}), ...(filters.to ? { to: filters.to } : {}) }}
          onChange={(window) => applyFilters({ from: window.from, to: window.to, offset: undefined })}
        />
        <SegmentControl
          label="Group by"
          value={group}
          options={GROUP_BYS}
          labels={{ model: 'Model', stage: 'Stage', agent: 'Agent', test: 'Test', date: 'Date' }}
          onChange={setGroup}
        />
        <SegmentControl
          label="Measure"
          value={selectedMeasure}
          options={MEASURES}
          labels={{ tokens: 'Tokens', cost: 'Cost', latency: 'Latency' }}
          disabled={{ cost: data !== undefined && !data.cost_visible }}
          onChange={setMeasure}
        />
        <input
          type="search"
          className="field-input analytics-project"
          placeholder="project..."
          aria-label="Filter by project"
          value={project}
          onChange={(event) => setProject(event.target.value)}
        />
        <input
          type="search"
          className="field-input analytics-test"
          placeholder="test or run id..."
          aria-label="Filter by test"
          value={test}
          onChange={(event) => setTest(event.target.value)}
        />
        <div className="level-chips analytics-status" role="group" aria-label="Status filter">
          {(['ok', 'error'] as const).map((status) => (
            <button
              key={status}
              type="button"
              className="level-chip"
              aria-pressed={filters.status === status}
              onClick={() =>
                applyFilters({
                  status: filters.status === status ? undefined : status,
                  offset: undefined,
                })
              }
            >
              {status}
            </button>
          ))}
        </div>
        {hasAnalyticsFilters(filters) && (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => {
              setProject('')
              setTest('')
              setSearchParams(new URLSearchParams())
            }}
          >
            Clear filters
          </button>
        )}
      </header>

      <section className="analytics-filter-pickers glass-panel" aria-label="Agent filters">
        <ChipGroup
          label="Models"
          options={MODEL_OPTIONS}
          selected={filters.model ?? []}
          onToggle={(value) => applyFilters({ model: toggleValue(filters.model, value), offset: undefined })}
        />
        <ChipGroup
          label="Stages"
          options={PHASE_NAMES}
          selected={filters.stage ?? []}
          onToggle={(value) => applyFilters({ stage: toggleValue(filters.stage, value), offset: undefined })}
        />
        <ChipGroup
          label="Agents"
          options={AGENT_OPTIONS}
          selected={filters.agent ?? []}
          onToggle={(value) => applyFilters({ agent: toggleValue(filters.agent, value), offset: undefined })}
        />
      </section>

      {isPending ? (
        <AnalyticsSkeleton />
      ) : isError && !data ? (
        <ProblemCard
          title="Agent analytics unavailable"
          message={errorMessage(error)}
          onRetry={() => refetch()}
        />
      ) : data ? (
        <>
          {isError && (
            <div className="analytics-stale-banner" role="alert" data-testid="analytics-stale-banner">
              Showing cached data — the latest refresh failed.{' '}
              <button type="button" className="analytics-link-button" onClick={() => refetch()}>
                Retry
              </button>
            </div>
          )}
          <AgentKpiCards data={data} allZeroTokens={allZeroTokens} />

          {allZeroTokens && (
            <div className="analytics-zero-hint" data-testid="analytics-zero-token-hint">
              Token capture begins with live LLM agents. Showing latency, runs, and error behavior for now.
            </div>
          )}

          {isEmptyWindow ? (
            <div className="dash-empty">
              <h2>No agent events in this window</h2>
              <p className="dash-empty-hint">Try a wider time window or clear a model, agent, or test filter.</p>
            </div>
          ) : (
            <>
              <AgentUsageCharts
                data={data}
                group={group}
                measure={effectiveMeasure}
                bucket={data.window.bucket}
              />
              {!allZeroTokens && <TokenSplitCharts data={data} />}
              <AgentBreakdownTable
                data={data}
                group={group}
                sort={sort}
                dir={dir}
                onSort={onSort}
                onFilter={addBreakdownFilter}
                onZoomDate={zoomDate}
                onPage={(offset) => applyFilters({ offset })}
              />
            </>
          )}
        </>
      ) : null}
    </section>
  )
}
