import { useMemo, useState } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import type { LiveEngineSample } from './liveTypes'

/**
 * Execution-phase live engine strip (D2, APEX Load Command Center pattern):
 * four command-metric pills from the latest engine_poll sample plus a Recharts
 * AreaChart over the rolling sample buffer (last CHART_WINDOW points), with a
 * metric tab per series. Pills and chart degrade per-metric with an em dash
 * when an engine reports no live_stats (plan risk: engine metric heterogeneity).
 *
 * Chart styling conventions follow Project_Stormrunner's TestExecutionCharts:
 * token-driven strokes (--chart-*, --text-muted, --border), tooltip on
 * --bg-elevated with --border, animations off for live data.
 */

export const CHART_WINDOW = 300

type MetricKey = 'vusers' | 'tps' | 'error_rate' | 'p95_ms'

interface MetricDef {
  key: MetricKey
  label: string
  color: string
  format: (value: number) => string
}

const EM_DASH = '—'

const METRICS: MetricDef[] = [
  { key: 'vusers', label: 'VUsers', color: 'var(--chart-1)', format: (v) => String(Math.round(v)) },
  {
    key: 'tps',
    label: 'TPS',
    color: 'var(--chart-2)',
    format: (v) => (Number.isInteger(v) ? String(v) : v.toFixed(1)),
  },
  {
    key: 'error_rate',
    label: 'Err %',
    color: 'var(--chart-4)',
    format: (v) => `${(v * 100).toFixed(2)}%`,
  },
  { key: 'p95_ms', label: 'p95', color: 'var(--chart-3)', format: (v) => `${Math.round(v)} ms` },
]

const TOOLTIP_CONTENT_STYLE: React.CSSProperties = {
  background: 'var(--bg-elevated)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  color: 'var(--text-primary)',
  fontSize: 11,
}

function metricValue(sample: LiveEngineSample | null | undefined, key: MetricKey): number | null {
  const value = sample?.live_stats?.[key]
  return typeof value === 'number' && !Number.isNaN(value) ? value : null
}

export function EngineStrip({
  samples,
  latest,
}: {
  samples: readonly LiveEngineSample[]
  latest?: LiveEngineSample | null
}) {
  const [metric, setMetric] = useState<MetricKey>('tps')
  const active = METRICS.find((candidate) => candidate.key === metric) ?? METRICS[1]!
  const latestSample = latest ?? samples[samples.length - 1] ?? null

  const data = useMemo(
    () =>
      samples.slice(-CHART_WINDOW).map((sample, index) => ({
        tick: index + 1,
        value: metricValue(sample, active.key),
      })),
    [samples, active.key],
  )

  return (
    <section className="engine-strip" aria-label="Live engine stats" data-testid="engine-strip">
      <div className="engine-strip-head">
        <span className="engine-strip-title">Engine</span>
        {latestSample?.status && (
          <span className="topbar-meta-chip accent" data-testid="engine-status">
            {latestSample.status}
            {typeof latestSample.progress_pct === 'number'
              ? ` · ${Math.round(latestSample.progress_pct)}%`
              : ''}
          </span>
        )}
      </div>

      <div className="kpi-row engine-pill-row">
        {METRICS.map((def) => {
          const value = metricValue(latestSample, def.key)
          return (
            <span key={def.key} className="kpi-pill" data-testid={`engine-pill-${def.key}`}>
              <span className="kpi-label">{def.label}</span>
              <span className="kpi-value">{value === null ? EM_DASH : def.format(value)}</span>
            </span>
          )
        })}
      </div>

      <div className="engine-metric-tabs" role="tablist" aria-label="Engine metrics">
        {METRICS.map((def) => (
          <button
            key={def.key}
            type="button"
            role="tab"
            aria-selected={metric === def.key}
            className="engine-metric-tab"
            onClick={() => setMetric(def.key)}
          >
            {def.label}
          </button>
        ))}
      </div>

      <div className="engine-chart" data-testid="engine-chart">
        <ResponsiveContainer width="100%" height={180}>
          <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="tick"
              stroke="var(--text-muted)"
              tick={{ fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              minTickGap={32}
            />
            <YAxis
              stroke="var(--text-muted)"
              tick={{ fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              width={44}
            />
            <Tooltip
              contentStyle={TOOLTIP_CONTENT_STYLE}
              isAnimationActive={false}
              labelFormatter={(tick) => `tick ${String(tick)}`}
              formatter={(value) => [
                typeof value === 'number' ? active.format(value) : EM_DASH,
                active.label,
              ]}
            />
            <Area
              type="monotone"
              dataKey="value"
              stroke={active.color}
              fill={active.color}
              fillOpacity={0.16}
              strokeWidth={2}
              dot={false}
              connectNulls
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </section>
  )
}
