import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { TableToolbar } from '@/components/ux/TableToolbar'
import { Pagination, type PageSelection } from '@/components/ux/Pagination'
import { SortableHeader } from '@/components/ux/SortableHeader'
import { CHART_TOOLTIP_STYLE } from '@/utils/chartTheme'
import { useCallback, useEffect, useId, useMemo, useState } from 'react'
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

import { formatTimestamp } from '@/utils/time'
import { PHASE_NAMES } from '@apex/pipeline-events'

import {
  useAgentAnalytics,
  type AgentAnalytics,
  type AgentAnalyticsBreakdownRow,
  type AgentOrder,
  type AgentSort,
} from '@/api/hooks/useAgentAnalytics'
import { isApiError } from '@/api/errors'
import { ProblemCard } from '@/components/ProblemCard'
import { WindowPresets } from '@/components/controls/WindowPresets'

import {
  ANALYTICS_MAX_OFFSET,
  DEFAULT_GROUP,
  DEFAULT_LIMIT,
  DEFAULT_MEASURE,
  GROUP_BYS,
  MEASURES,
  defaultSortFor,
  effectiveBucket,
  parseAnalyticsFilters,
  serializeAnalyticsFilters,
  type AnalyticsFilters,
  type Bucket,
  type GroupBy,
  type Measure,
} from './analyticsFilters'
import { aggregateMeasurements, latencySamples, measurementSeries, metricValue, rankedMeasurements } from './analyticsMeasurements'
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
/** Bar chart shows the top N breakdown rows; the table page size can exceed this. */
const TOP_N_BARS = 10
const AGENT_OPTIONS = PHASE_NAMES.map((phase) => `${phase}.worker`)


function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function bucketTick(bucket: Bucket): (iso: string) => string {
  return (iso) => {
    const date = new Date(iso)
    if (Number.isNaN(date.getTime())) return iso
    return bucket === 'hour'
      ? `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`
      : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
  }
}

function truncate(value: string, max = 24): string {
  return value.length > max ? `${value.slice(0, max - 1)}...` : value
}

function labelForGroup(group: GroupBy): string {
  return {
    model: 'Model',
    stage: 'Phase',
    agent: 'Worker agent',
    test: 'Run',
    date: 'Date',
  }[group]
}

function labelForMeasure(measure: Measure): string {
  return { tokens: 'Tokens', cost: 'Cost', latency: 'Average latency' }[measure]
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
  return <Skeleton label="Loading agent analytics" rows={5} />
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
            aria-label={option}
            title={option}
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
    limit: filters.limit ?? DEFAULT_LIMIT,
    offset: filters.offset ?? 0,
  }
}

function AgentKpiCards({
  data,
  allZeroTokens,
}: {
  data: AgentAnalytics
  allZeroTokens: boolean
}) {
  const empty = data.totals.events === 0
  const errorRate = formatPercent(data.totals.errors, data.totals.events)
  return (
    <div className="stat-cards" data-testid="analytics-cards">
      <StatCard
        label="Total tokens"
        value={empty || allZeroTokens ? '—' : formatTokens(data.totals.total_tokens)}
        hint={empty ? 'No events in this window' : allZeroTokens ? 'Token usage not recorded' : `${formatTokens(data.totals.input_tokens)} in / ${formatTokens(data.totals.output_tokens)} out`}
        testId="stat-total-tokens"
      />
      <StatCard label="Est. cost" value={empty || !data.cost_visible ? '—' : formatCost(data.totals.cost_usd)} hint={!data.cost_visible ? 'Not available for your role' : undefined} testId="stat-cost" />
      <StatCard
        label="Avg latency"
        value={empty ? '—' : formatLatency(data.totals.avg_latency_ms)}
        hint={empty ? 'No events in this window' : `P95 ${formatLatency(data.totals.p95_latency_ms)}`}
        testId="stat-latency"
      />
      <StatCard
        label="Agents / runs"
        value={empty ? '—' : `${data.totals.agents} agents · ${data.totals.runs} runs`}
        hint={empty ? 'No events in this window' : data.totals.models === 0 ? 'Model not recorded' : `${data.totals.models} recorded models`}
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
  const patternPrefix = useId().replaceAll(':', '')
  const stacked = useMemo(() => measurementSeries(data.series, measure, group), [data.series, measure, group])
  const bars = useMemo(() => rankedMeasurements(data.series, measure).slice(0, TOP_N_BARS).map(row => ({ ...row, label: truncate(row.key) })), [data.series, measure])

  if (measure === 'latency' && data.series.some(row => metricValue(row, measure) !== null && latencySamples(row) === null)) {
    return <StateCard kind="unconfigured" title="Latency aggregation unavailable" action={null} compact>
      <p>Recorded averages remain in the table. This response has no latency sample counts, so combined averages and rankings cannot be calculated reliably.</p>
    </StateCard>
  }
  if (bars.length === 0) {
    return <StateCard kind="empty" title={`No recorded ${labelForMeasure(measure).toLowerCase()} measurements`} action={null} compact>
      <p>The chart series contain no recorded values for this measurement. Missing values are not plotted as zero.</p>
    </StateCard>
  }

  return (
    <div className="analytics-charts">
      <section className="glass-panel chart-panel" data-testid="analytics-agent-series-chart">
        <h2 className="chart-title">
          {labelForMeasure(measure)} over time
          {group === 'date' ? '' : ` by ${labelForGroup(group).toLowerCase()}`}
        </h2>
        {data.series.some(row => metricValue(row, measure) === null) && <p className="ux-context">Charts include recorded measurements only. Missing values appear as gaps and are excluded from ranking.</p>}
        <ResponsiveContainer width="100%" height={240}>
          <AreaChart data={stacked.series} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <defs>{CHART_COLORS.map((color, index) => <pattern key={color} id={`${patternPrefix}-${index}`} width={4 + index * 2} height={4 + index * 2} patternUnits="userSpaceOnUse" patternTransform={`rotate(${index * 30})`}><line x1="0" y1="0" x2="0" y2="20" stroke={color} strokeWidth="2" /></pattern>)}</defs>
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
              contentStyle={CHART_TOOLTIP_STYLE}
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
                legendType="plainline"
                stackId={measure === 'latency' ? undefined : 'agent'}
                stroke={CHART_COLORS[index % CHART_COLORS.length]}
                fill={`url(#${patternPrefix}-${index % CHART_COLORS.length})`}
                fillOpacity={0.16}
                strokeWidth={2}
                strokeDasharray={['', '8 3', '3 3', '10 3 2 3', '2 5', '12 4'][index]}
                connectNulls={false}
                dot={{ r: 2 + index % 3 }}
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
              contentStyle={CHART_TOOLTIP_STYLE}
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
    const buckets = new Map<string, { bucket_start: string; input: number; output: number; costRows: AgentAnalytics['series'] }>()
    for (const row of data.series) {
      const bucket = buckets.get(row.bucket_start) ?? {
        bucket_start: row.bucket_start,
        input: 0,
        output: 0,
        costRows: [],
      }
      bucket.input += row.input_tokens
      bucket.output += row.output_tokens
      bucket.costRows.push(row)
      buckets.set(row.bucket_start, bucket)
    }
    return Array.from(buckets.values(), ({ costRows, ...bucket }) => ({ ...bucket, cost: aggregateMeasurements(costRows, 'cost') }))
      .sort((a, b) => a.bucket_start.localeCompare(b.bucket_start))
  }, [data.series])

  return (
    <div className="analytics-charts analytics-charts-secondary">
      <section className="glass-panel chart-panel" data-testid="analytics-token-split-chart">
        <h2 className="chart-title">Input vs output tokens (top series)</h2><p className="ux-context">Input: solid line. Output: dashed line with markers.</p>
        <ResponsiveContainer width="100%" height={200}>
          <AreaChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="bucket_start" tickFormatter={bucketTick(data.window.bucket)} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} />
            <YAxis tickFormatter={(value) => formatTokens(Number(value))} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} width={54} />
            <Tooltip contentStyle={CHART_TOOLTIP_STYLE} isAnimationActive={false} formatter={(value) => formatTokens(Number(value))} />
            <Area type="monotone" dataKey="input" name="input" stroke="var(--chart-1)" fill="var(--chart-1)" fillOpacity={0.16} dot={false} isAnimationActive={false} />
            <Area type="monotone" dataKey="output" name="output" strokeDasharray="8 4" stroke="var(--chart-5)" fill="var(--chart-5)" fillOpacity={0.12} dot={{ r: 3 }} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </section>
      {data.cost_visible && (
        <section className="glass-panel chart-panel" data-testid="analytics-cost-trend-chart">
          <h2 className="chart-title">Cost trend (top series)</h2>
          {rows.some(row => row.cost !== null) ? <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="bucket_start" tickFormatter={bucketTick(data.window.bucket)} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} />
              <YAxis tickFormatter={(value) => formatCost(Number(value))} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} width={54} />
              <Tooltip contentStyle={CHART_TOOLTIP_STYLE} isAnimationActive={false} formatter={(value) => formatCost(Number(value))} />
              <Area type="monotone" dataKey="cost" name="cost" stroke="var(--chart-3)" fill="var(--chart-3)" fillOpacity={0.14} dot={false} connectNulls={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer> : <StateCard kind="empty" title="No recorded cost measurements" action={null} compact><p>Cost was not recorded for these events.</p></StateCard>}
        </section>
      )}
    </div>
  )
}

function nextSort(current: AgentSort, currentDir: AgentOrder, next: AgentSort): AgentOrder {
  if (current !== next) return 'desc'
  return currentDir === 'desc' ? 'asc' : 'desc'
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
  onPage: (page: PageSelection) => void
}) {
  const page = data.page
  const nextDisabled =
    page.offset >= ANALYTICS_MAX_OFFSET || page.offset + page.limit >= page.total
  const reachedResultWindow =
    page.offset >= ANALYTICS_MAX_OFFSET &&
    page.offset + data.breakdown.length < page.total
  const caption =
    page.total === 0
      ? 'No rows'
      : `${page.offset + 1}-${Math.min(page.offset + data.breakdown.length, page.total)} of ${page.total}`

  function SortHead({ id, children }: { id: AgentSort; children: React.ReactNode }) {
    return <SortableHeader active={sort === id} direction={dir} onSort={() => onSort(id)} className="analytics-sort">{children}</SortableHeader>
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
      {reachedResultWindow && (
        <p className="analytics-result-window-note" role="status">
          Reached the analytics result-window limit. Refine the filters to inspect later rows.
        </p>
      )}
      <footer className="analytics-pagination">
        <span className="analytics-pagination-caption">{caption}</span>
        <Pagination label="Analytics" offset={page.offset} limit={page.limit} options={[10, 20, 50, 100]}
          hasNext={!nextDisabled} pending={false} onChange={({ offset, limit }) => onPage({ offset: Math.min(ANALYTICS_MAX_OFFSET, offset), limit })} />
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
        {new Date(row.key).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })}
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
  useTimestampPreference()
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
  const modelQuery = useMemo(() => {
    return { ...query, model: undefined, offset: 0, group_by: 'model' as const, limit: 100, sort: 'key' as const }
  }, [query])
  const models = useAgentAnalytics(modelQuery)
  const modelOptions = [...new Set([...(models.data?.breakdown.map(row => row.key) ?? []), ...(filters.model ?? [])])].sort()


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
      <TableToolbar label="Analytics filters" filterCount={[filters.from || filters.to, filters.project, filters.model?.length, filters.stage?.length, filters.agent?.length, filters.test, filters.status].filter(Boolean).length} onClear={() => { setProject(''); setTest(''); setSearchParams(new URLSearchParams()) }} className="analytics-toolbar glass-panel">
        <WindowPresets
          value={{ ...(filters.from || data?.window.from ? { from: filters.from ?? data?.window.from } : {}), ...(filters.to || data?.window.to ? { to: filters.to ?? data?.window.to } : {}) }}
          onChange={(window) => applyFilters({ from: window.from, to: window.to, offset: undefined })}
        />
        <SegmentControl
          label="Group by"
          value={group}
          options={GROUP_BYS}
          labels={{ model: 'Model', stage: 'Phase', agent: 'Agent', test: 'Run', date: 'Date' }}
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
          placeholder="Run ID…"
          aria-label="Filter by run"
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

      </TableToolbar>

      {data && <p className="ux-context">{formatTimestamp(data.window.from)} – {formatTimestamp(data.window.to)} · {data.window.bucket} buckets in UTC. All metrics cover events recorded in this window. Worker latency excludes review waits for new events; older events may include reviews.</p>}
      <section className="analytics-filter-pickers glass-panel" aria-label="Agent filters">
        <ChipGroup
          label="Models"
          options={modelOptions}
          selected={filters.model ?? []}
          onToggle={(value) => applyFilters({ model: toggleValue(filters.model, value), offset: undefined })}
        />
        <ChipGroup
          label="Phases"
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
            <StateCard kind="filtered" title="No agent events in this window" action={<button type="button" className="btn btn-secondary" onClick={() => { setProject(''); setTest(''); setSearchParams(new URLSearchParams()) }}>Reset window and filters</button>}>
              <p>Try a wider time window or clear a model, agent, or run filter.</p>
            </StateCard>
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
                onPage={(page) => applyFilters(page)}
              />
            </>
          )}
        </>
      ) : null}
    </section>
  )
}
