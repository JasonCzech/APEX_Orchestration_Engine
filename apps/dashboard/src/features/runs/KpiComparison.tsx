import type { TestResultSummary } from '@apex/pipeline-events'

import { engineLabel, engineVerdict, isMinimumThreshold, judgesPerRow, thresholdMet } from './acceptanceThresholds'

/** Display rules for the KPIs engines report; unknown keys fall back to raw numbers. */
const METRICS: Record<string, { label: string; unit?: string; scale?: number; minimum?: boolean }> = {
  p95_ms: { label: 'P95 response time', unit: 'ms' },
  error_rate: { label: 'Error rate', unit: '%', scale: 100 },
  tps_avg: { label: 'Average throughput', unit: 'TPS', minimum: true },
  vusers_peak: { label: 'Peak VUsers' },
}
const ORDER = Object.keys(METRICS)

function metric(key: string) {
  return METRICS[key] ?? { label: key.replaceAll('_', ' '), minimum: isMinimumThreshold(key) }
}

function format(key: string, value: number) {
  const { unit, scale = 1 } = metric(key)
  const scaled = value * scale
  const text = Number.isInteger(scaled) ? scaled.toLocaleString() : scaled.toLocaleString(undefined, { maximumFractionDigits: 2 })
  return unit ? `${text} ${unit}` : text
}

/**
 * Requested acceptance thresholds beside observed KPIs, the core answer of a
 * load test. Thresholds are the ones the engine applied, not the draft's.
 */
export function KpiComparison({ summary, thresholds, caption }: {
  summary: TestResultSummary
  thresholds?: Record<string, number> | null
  caption?: string
}) {
  const kpis = summary.kpis ?? {}
  const limits = thresholds ?? {}
  const keys = [...new Set([...Object.keys(limits), ...Object.keys(kpis)])]
    .sort((a, b) => (ORDER.indexOf(a) + 1 || ORDER.length + 1) - (ORDER.indexOf(b) + 1 || ORDER.length + 1))
  if (keys.length === 0) return null
  const perRow = judgesPerRow(summary)
  const rows = keys.map(key => {
    const { label, minimum } = metric(key)
    const limit = limits[key]
    const observed = kpis[key]
    const met = perRow ? thresholdMet(key, limit, observed) : null
    return { key, label, minimum, limit, observed, met }
  })
  const verdict = engineVerdict(summary)
  const disagrees = perRow && summary.passed === false && !rows.some(row => row.met === false)
  const engine = engineLabel(summary.engine)
  return (
    <div className="kpi-comparison" role="group" aria-label="Execution results">
      {!perRow && <p><strong>Engine verdict: {verdict}.</strong> {engine ?? 'The engine'} assessed these thresholds with its own measurements.</p>}
      <div className="data-table-wrap">
        <table className="data-table">
          <caption className="sr-only">{caption ?? 'Requested acceptance thresholds and observed results'}</caption>
          <thead>
            <tr><th scope="col">Metric</th><th scope="col">Threshold</th><th scope="col">Observed</th>{perRow && <th scope="col">Result</th>}</tr>
          </thead>
          <tbody>
            {rows.map(({ key, label, minimum, limit, observed, met }) => (
              <tr key={key}>
                <th scope="row">{label}</th>
                <td>{limit === undefined ? <span className="ux-context">None set</span> : `${minimum ? '≥' : '≤'} ${format(key, limit)}`}</td>
                <td>{observed === undefined ? <span className="ux-context">Not reported</span> : format(key, observed)}</td>
                {perRow && <td>{met === null ? <span className="ux-context"><span aria-hidden="true">—</span><span className="sr-only">Not evaluated</span></span> : <span className={`status-badge ${met ? 'success' : 'danger'}`}>{met ? 'Met' : 'Not met'}</span>}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {disagrees && <p className="tonal-card warning">The engine reported a failure{summary.sla_breaches?.length ? `: ${summary.sla_breaches.join('; ')}` : ''}. Its verdict takes precedence over this table.</p>}
      {summary.engine === 'sim' && <p className="ux-context">Simulated engine — KPIs are synthetic.</p>}
    </div>
  )
}
